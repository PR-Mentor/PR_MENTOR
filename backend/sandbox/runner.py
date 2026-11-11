"""Trusted in-image pytest launcher with separately bounded output streams."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--max-output-bytes", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("test_path")
    arguments = parser.parse_args()
    if (
        not 1_024 <= arguments.max_output_bytes <= 1_000_000
        or not 1 <= arguments.timeout_seconds <= 120
    ):
        return 125
    target = Path(arguments.test_path)
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        return 125
    if not resolved.is_file() or not resolved.is_relative_to(Path("/input").resolve()):
        return 125

    command = [
        sys.executable,
        "-I",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-c",
        "/dev/null",
        "--noconftest",
        "--import-mode=prepend",
        str(resolved),
    ]
    child = subprocess.Popen(
        command,
        cwd="/input",
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
        start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    used = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def drain(name: str, stream) -> None:
        nonlocal used
        while True:
            chunk = stream.read(16_384)
            if not chunk:
                return
            with lock:
                remaining = arguments.max_output_bytes - used
                if remaining > 0:
                    kept = chunk[:remaining]
                    output[name].extend(kept)
                    used += len(kept)
                if len(chunk) > remaining:
                    overflow.set()
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    readers = [
        threading.Thread(target=drain, args=("stdout", child.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", child.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        return_code = child.wait(timeout=arguments.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return_code = child.wait()
    for reader in readers:
        reader.join(timeout=2)
    if any(reader.is_alive() for reader in readers):
        return_code = 125
        overflow.set()
    payload = {
        "exit_code": None
        if overflow.is_set() or timed_out or return_code < 0
        else return_code,
        "stdout": bytes(output["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(output["stderr"]).decode("utf-8", errors="replace"),
        "output_truncated": overflow.is_set(),
        "timed_out": timed_out,
        "resource_limit_reason": (
            "output_limit"
            if overflow.is_set()
            else "wall_clock_timeout"
            if timed_out
            else None
        ),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
