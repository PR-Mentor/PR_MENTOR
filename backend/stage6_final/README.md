# PR Mentor Stage 6 — final infrastructure batch CLI

This package configures **rootful Docker + gVisor** on the dedicated Ubuntu 24.04 VM, builds the pinned starter execution image, and runs real runtime/isolation/resource/lifecycle checks.

## Use

Run from the Ubuntu VM over your existing Windows PowerShell SSH window. Do not use the VirtualBox console for copy/paste.

```bash
cd ~/stage6-final
chmod +x stage6 scripts/*.sh
./stage6 setup
./stage6 verify
```

Or run everything sequentially:

```bash
./stage6 audit
```

Run as normal user `maan`, **not** with `sudo`. The scripts invoke sudo only for host-level operations.

## What it does

- Installs gVisor if missing.
- Backs up and merges `/etc/docker/daemon.json` to register `/usr/bin/runsc` with the rootful daemon.
- Restarts rootful Docker and checks runtime registration.
- Builds `pr-mentor-stage6-executor:0.1.0` from a Python 3.12 slim base pinned by digest and installs pytest 8.3.5.
- Runs a real `runsc` container with network disabled, read-only root filesystem, non-root UID, dropped capabilities, no-new-privileges, and configured resource limits.
- Checks Docker's configured memory/swap/CPU/PID values.
- Attempts actual PID and memory exhaustion tests.
- Checks network isolation and read-only filesystem behavior.
- Checks Docker stop/remove cleanup.

## Important limitations — do not overclaim

A successful run is evidence for the host/runtime controls exercised by these scripts. It is **not** Stage 6 certification and does not implement the Python worker. In particular:
- CPU limit is verified as a Docker-applied configuration value, not by a calibrated CPU stress benchmark.
- Memory/PID enforcement tests must pass on the actual VM; a configured value alone is not proof.
- The cleanup test verifies Docker lifecycle cleanup, not worker-level wall-clock timeout/cancellation.
- Worker input validation, output bounds, concurrency controls, safe artifact handling, crash recovery, and Python worker tests remain implementation work.
- Stage 10 still owns API/persistence/queue/frontend integration.
- Do not use `--ignore-cgroups` to make gVisor start; that would undermine resource enforcement.
- Rootful Docker's socket is privileged. Keep access restricted to trusted administrators; never expose it to the worker or sandbox.

If any script fails, stop at that failure and retain its full output. Do not disable security controls to get a green result.
