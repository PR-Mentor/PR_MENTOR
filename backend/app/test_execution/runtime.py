"""Replaceable runtime contract and rootless Docker + gVisor implementation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import uuid4

from app.test_execution.contracts import ExecutionResourceLimits
from app.verification.bounded_process import BoundedProcessError, run_bounded_process


class RuntimeUnavailable(RuntimeError):
    """Required isolated-runtime prerequisite is missing or unsafe."""


class RuntimeInfrastructureError(RuntimeError):
    """Runtime operation failed independently of the test result."""

    def __init__(
        self,
        message: str,
        *,
        cleanup_confirmed: bool = True,
        exit_code: int | None = None,
        resource_limit_reason: str | None = None,
    ):
        super().__init__(message)
        self.cleanup_confirmed = cleanup_confirmed
        self.exit_code = exit_code
        self.resource_limit_reason = resource_limit_reason


@dataclass(frozen=True)
class RuntimeMetadata:
    name: str
    version: str
    image_digest: str
    python_version: str
    pytest_version: str


@dataclass(frozen=True)
class RuntimeHandle:
    runtime_id: str
    metadata: RuntimeMetadata
    input_directory: Path
    started_monotonic: float
    output_bytes: int


@dataclass(frozen=True)
class RuntimeCompletion:
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool = False
    streams_merged: bool = False
    timed_out: bool = False
    cancelled: bool = False
    resource_limit_reason: str | None = None


class ExecutionRuntime(Protocol):
    """Lifecycle contract independent of a particular container runtime."""

    def preflight(self) -> RuntimeMetadata: ...

    def create(
        self,
        input_directory: Path,
        *,
        source_relative_path: str,
        test_relative_path: str,
        execution_id: str,
        limits: ExecutionResourceLimits,
    ) -> RuntimeHandle: ...

    def wait(
        self,
        handle: RuntimeHandle,
        *,
        timeout_seconds: int,
        cancellation: Event,
    ) -> RuntimeCompletion: ...

    def collect(self, handle: RuntimeHandle) -> RuntimeCompletion: ...

    def cancel(self, handle: RuntimeHandle) -> None: ...

    def cleanup(self, handle: RuntimeHandle) -> bool: ...


class DockerGVisorRuntime:
    """Rootless Docker adapter that refuses every non-gVisor execution mode."""

    name = "rootless-docker-gvisor"

    def __init__(self, *, image_digest: str, docker_executable: str = "docker"):
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image_digest):
            raise ValueError(
                "execution image must be pinned by a lowercase SHA-256 digest"
            )
        self.image_digest = image_digest
        docker = shutil.which(docker_executable)
        if docker is None:
            raise RuntimeUnavailable(
                "Docker CLI is unavailable on the dedicated worker."
            )
        self.docker: str = docker

    def preflight(self) -> RuntimeMetadata:
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise RuntimeUnavailable("cgroups v2 is unavailable.")
        try:
            controllers = Path("/sys/fs/cgroup/cgroup.controllers").read_text(
                encoding="ascii"
            )
        except OSError as exc:
            raise RuntimeUnavailable("cgroups v2 controllers cannot be read.") from exc
        if not controllers.strip():
            raise RuntimeUnavailable("cgroups v2 reports no available controllers.")

        info = self._preflight_call(
            [
                "info",
                "--format",
                "{{json .SecurityOptions}}|{{.CgroupVersion}}|{{json .Runtimes}}",
            ]
        )
        pieces = info.strip().split("|", maxsplit=2)
        if len(pieces) != 3:
            raise RuntimeUnavailable(
                "Docker returned malformed security/runtime metadata."
            )
        try:
            security = json.loads(pieces[0])
            runtimes = json.loads(pieces[2])
        except json.JSONDecodeError as exc:
            raise RuntimeUnavailable(
                "Docker security/runtime metadata is malformed."
            ) from exc
        if pieces[1] != "2" or not any(
            "rootless" in str(item).casefold() for item in security
        ):
            raise RuntimeUnavailable("Docker must be rootless and use cgroups v2.")
        if not isinstance(runtimes, dict) or "runsc" not in runtimes:
            raise RuntimeUnavailable("Docker gVisor runtime runsc is unavailable.")

        image_labels = self._preflight_call(
            [
                "image",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                self.image_digest,
            ]
        )
        try:
            labels = json.loads(image_labels)
        except json.JSONDecodeError as exc:
            raise RuntimeUnavailable(
                "Pinned execution image labels are malformed."
            ) from exc
        if not isinstance(labels, dict):
            raise RuntimeUnavailable(
                "Pinned execution image lacks required version labels."
            )
        python_version = labels.get("org.prmentor.python-version")
        pytest_version = labels.get("org.prmentor.pytest-version")
        if not isinstance(python_version, str) or not isinstance(pytest_version, str):
            raise RuntimeUnavailable(
                "Pinned execution image lacks Python/pytest version labels."
            )
        server_version = self._preflight_call(
            ["version", "--format", "{{.Server.Version}}"]
        ).strip()
        if not server_version:
            raise RuntimeUnavailable("Docker server version is unavailable.")
        return RuntimeMetadata(
            self.name, server_version, self.image_digest, python_version, pytest_version
        )

    def create(
        self,
        input_directory: Path,
        *,
        source_relative_path: str,
        test_relative_path: str,
        execution_id: str,
        limits: ExecutionResourceLimits,
    ) -> RuntimeHandle:
        metadata = self.preflight()
        root = input_directory.resolve(strict=True)
        if not root.is_dir():
            raise RuntimeInfrastructureError(
                "Input artifact directory is not a directory."
            )
        name = "prmentor-test-" + uuid4().hex
        scratch_mib = max(4, limits.scratch_bytes // (2 * 1024 * 1024))
        cpu_quota = limits.cpu_count * 100_000
        command = [
            self.docker,
            "run",
            "--detach",
            "--pull=never",
            "--name",
            name,
            "--label=org.prmentor.execution=phase4-stage6",
            "--label=org.prmentor.execution-id="
            + hashlib.sha256(execution_id.encode()).hexdigest()[:24],
            "--runtime=runsc",
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            str(limits.process_count),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--shm-size",
            f"{scratch_mib}m",
            "--cpu-period=100000",
            "--cpu-quota",
            str(cpu_quota),
            "--mount",
            f"type=bind,src={root},dst=/input,readonly",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={scratch_mib}m,mode=1777",
            "--workdir=/input",
            "--env=HOME=/tmp",
            "--env=TMPDIR=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            self.image_digest,
            "python",
            "-I",
            "/opt/prmentor/runner.py",
            "--max-output-bytes",
            str(limits.output_bytes),
            "--timeout-seconds",
            str(limits.timeout_seconds),
            "/input/" + test_relative_path,
        ]
        try:
            self._call(command[1:], 10)
        except (RuntimeUnavailable, RuntimeInfrastructureError) as exc:
            if not self._remove_container(name):
                raise RuntimeInfrastructureError(
                    "Container startup failed and cleanup could not be confirmed.",
                    cleanup_confirmed=False,
                ) from exc
            raise RuntimeInfrastructureError(
                "Sandbox container startup failed."
            ) from exc
        return RuntimeHandle(
            name, metadata, root, time.monotonic(), limits.output_bytes
        )

    def wait(
        self,
        handle: RuntimeHandle,
        *,
        timeout_seconds: int,
        cancellation: Event,
    ) -> RuntimeCompletion:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if cancellation.is_set():
                return RuntimeCompletion(None, "", "", cancelled=True)
            if time.monotonic() >= deadline:
                return RuntimeCompletion(
                    None,
                    "",
                    "",
                    timed_out=True,
                    resource_limit_reason="wall_clock_timeout",
                )
            running = self._call(
                ["inspect", "--format", "{{.State.Running}}", handle.runtime_id], 5
            )
            if running.strip().casefold() == "false":
                return self.collect(handle)
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def collect(self, handle: RuntimeHandle) -> RuntimeCompletion:
        try:
            wait_result = self._call(["wait", handle.runtime_id], 5).strip()
            if not re.fullmatch(r"-?\d+", wait_result):
                raise RuntimeInfrastructureError(
                    "Docker returned an invalid container exit code."
                )
            container_exit_code = int(wait_result)
            if container_exit_code != 0:
                try:
                    state = (
                        self._call(
                            [
                                "inspect",
                                "--format",
                                "{{.State.OOMKilled}}",
                                handle.runtime_id,
                            ],
                            3,
                        )
                        .strip()
                        .casefold()
                    )
                except (RuntimeUnavailable, RuntimeInfrastructureError):
                    state = ""
                raise RuntimeInfrastructureError(
                    f"Sandbox launcher exited unexpectedly with code {container_exit_code}.",
                    exit_code=container_exit_code,
                    resource_limit_reason="memory_limit" if state == "true" else None,
                )
            return self._bounded_logs(handle.runtime_id, handle.output_bytes)
        except (RuntimeUnavailable, RuntimeInfrastructureError):
            raise
        except Exception as exc:  # Docker adapter failures are infrastructure errors.
            raise RuntimeInfrastructureError(
                "Container result collection failed."
            ) from exc

    def cancel(self, handle: RuntimeHandle) -> None:
        try:
            self._call(["kill", handle.runtime_id], 5)
        except RuntimeUnavailable:
            # cleanup() still makes a best effort to forcibly remove it.
            pass

    def cleanup(self, handle: RuntimeHandle) -> bool:
        return self._remove_container(handle.runtime_id)

    def _remove_container(self, name: str) -> bool:
        try:
            self._call(["rm", "--force", "--volumes", name], 5)
        except Exception:  # noqa: BLE001 - cleanup must verify unknown CLI failures.
            try:
                self._call(["inspect", name], 3)
            except Exception as inspect_error:  # noqa: BLE001 - classify absence vs uncertainty.
                return (
                    "no such container" in str(inspect_error).casefold()
                    or "not found" in str(inspect_error).casefold()
                )
            return False
        try:
            self._call(["inspect", name], 3)
        except Exception as inspect_error:  # noqa: BLE001 - absence is expected after removal.
            return (
                "no such container" in str(inspect_error).casefold()
                or "not found" in str(inspect_error).casefold()
            )
        return False

    def _preflight_call(self, args: list[str]) -> str:
        try:
            return self._call(args, 5)
        except (RuntimeUnavailable, RuntimeInfrastructureError) as exc:
            raise RuntimeUnavailable(
                "Rootless Docker or the pinned sandbox image is unavailable."
            ) from exc

    def _bounded_logs(self, name: str, output_bytes: int) -> RuntimeCompletion:
        try:
            result = run_bounded_process(
                [self.docker, "logs", name],
                cwd=Path.cwd(),
                env=self._worker_environment(),
                timeout=5,
                max_stdout_bytes=max(8_192, output_bytes * 8 + 4_096),
                max_stderr_bytes=4_096,
            )
        except BoundedProcessError as exc:
            raise RuntimeInfrastructureError(
                "Container output exceeded bounds or could not be read."
            ) from exc
        try:
            payload = json.loads(result.stdout.decode("utf-8", errors="strict"))
            exit_code = payload["exit_code"]
            stdout = payload["stdout"]
            stderr = payload["stderr"]
            output_truncated = payload["output_truncated"]
            timed_out = payload["timed_out"]
            resource_reason = payload["resource_limit_reason"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeInfrastructureError(
                "Sandbox launcher returned malformed output."
            ) from exc
        if (
            (
                exit_code is not None
                and (not isinstance(exit_code, int) or isinstance(exit_code, bool))
            )
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or not isinstance(output_truncated, bool)
            or not isinstance(timed_out, bool)
            or (resource_reason is not None and not isinstance(resource_reason, str))
        ):
            raise RuntimeInfrastructureError(
                "Sandbox launcher returned invalid result fields."
            )
        return RuntimeCompletion(
            exit_code,
            stdout,
            stderr,
            output_truncated=output_truncated,
            timed_out=timed_out,
            resource_limit_reason=resource_reason,
        )

    def _call(self, args: list[str], timeout: int) -> str:
        if self.docker is None:
            raise RuntimeUnavailable("Docker CLI is unavailable.")
        try:
            result = subprocess.run(
                [self.docker, *args],
                cwd=Path.cwd(),
                env=self._worker_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeUnavailable(
                "Docker runtime operation could not complete."
            ) from exc
        if result.returncode:
            message = result.stderr[:2_000].decode("utf-8", errors="replace")
            error = f"Docker runtime operation failed: {message or 'unknown error'}"
            if (
                "no such container" in message.casefold()
                or "not found" in message.casefold()
            ):
                raise RuntimeUnavailable(error)
            raise RuntimeInfrastructureError(error)
        return result.stdout[:8_192].decode("utf-8", errors="replace")

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
        for key in ("DOCKER_HOST", "XDG_RUNTIME_DIR"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env
