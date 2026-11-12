"""Bounded subprocess execution for static analyzers.

This limits runtime, captured output, and simultaneous processes. It is not an
OS security sandbox; analyzer subprocesses must never receive repository
credentials or execute repository code.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_ACTIVE_ANALYZERS = 2
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
_PROCESS_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_ANALYZERS)


class BoundedProcessError(RuntimeError):
    """A process was unavailable, timed out, or exceeded bounded output."""

    def __init__(self, message: str, *, inconclusive: bool = False):
        super().__init__(message)
        self.inconclusive = inconclusive


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    cancelled: bool = False


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float,
    max_stdout_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_stderr_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    cancellation_check: Callable[[], bool] | None = None,
) -> BoundedProcessResult:
    """Run an argument-array command with capped capture and hard timeout."""
    if (
        not command
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or isinstance(max_stdout_bytes, bool)
        or not isinstance(max_stdout_bytes, int)
        or max_stdout_bytes < 1
        or isinstance(max_stderr_bytes, bool)
        or not isinstance(max_stderr_bytes, int)
        or max_stderr_bytes < 1
    ):
        raise ValueError("command, timeout, and output limits must be positive")
    root = Path(cwd).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("working directory must be an existing directory")
    if not _PROCESS_SLOTS.acquire(timeout=timeout):
        raise BoundedProcessError(
            "Analyzer concurrency limit was reached.", inconclusive=True
        )

    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    readers: list[threading.Thread] = []
    output: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    overflow: set[str] = set()
    read_errors: list[Exception] = []

    def drain(name: str, stream, limit: int) -> None:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                remaining = limit - len(output[name])
                if remaining > 0:
                    output[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.add(name)
        except (OSError, ValueError) as exc:  # captured and raised in the caller thread
            read_errors.append(exc)
        finally:
            stream.close()

    try:
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        )
        process = subprocess.Popen(
            list(command),
            cwd=str(root),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(
                target=drain,
                args=("stdout", process.stdout, max_stdout_bytes),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=("stderr", process.stderr, max_stderr_bytes),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        deadline = started + timeout
        cancelled = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                raise BoundedProcessError(
                    f"Analyzer timed out after {timeout:g} seconds.", inconclusive=True
                )
            try:
                returncode = process.wait(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                if cancellation_check is None or not cancellation_check():
                    continue
                cancelled = True
                _request_process_cancel(process)
                try:
                    returncode = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _terminate_process(process)
                    returncode = process.wait(timeout=2)
                break
        for reader in readers:
            reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers):
            _terminate_process(process)
            raise BoundedProcessError(
                "Analyzer output pipes did not close after process exit.",
                inconclusive=True,
            )
        if read_errors:
            raise BoundedProcessError(
                "Analyzer output could not be read.", inconclusive=True
            )
        if overflow:
            streams = " and ".join(sorted(overflow))
            raise BoundedProcessError(
                f"Analyzer exceeded the {streams} output limit.", inconclusive=True
            )
        if returncode < 0:
            raise BoundedProcessError(
                f"Analyzer terminated by signal {-returncode}.", inconclusive=True
            )
        return BoundedProcessResult(
            returncode=returncode,
            stdout=bytes(output["stdout"]),
            stderr=bytes(output["stderr"]),
            elapsed_seconds=time.monotonic() - started,
            cancelled=cancelled,
        )
    except BoundedProcessError:
        raise
    except (OSError, ValueError) as exc:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        raise BoundedProcessError(
            "Analyzer process could not be started or managed."
        ) from exc
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        for reader in readers:
            reader.join(timeout=1)
        _PROCESS_SLOTS.release()


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        kill_process_group = getattr(os, "killpg", None)
        force_kill = getattr(signal, "SIGKILL", None)
        if os.name != "nt" and callable(kill_process_group) and force_kill is not None:
            kill_process_group(process.pid, force_kill)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _request_process_cancel(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        terminate_process_group = getattr(os, "killpg", None)
        terminate_signal = getattr(signal, "SIGTERM", None)
        if os.name != "nt" and callable(terminate_process_group) and terminate_signal is not None:
            terminate_process_group(process.pid, terminate_signal)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
