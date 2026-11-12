# Stage 6 Batch CLI — Ubuntu 24.04

Commands: `./stage6 audit`, `./stage6 setup`, `./stage6 verify`.

Run as the normal worker user (`maan`), not root. `audit` is read-only. `setup` installs packages, configures rootless Docker and gVisor, and builds a starter pytest image. `verify` runs disposable container smoke tests.

Copy this folder into the PR_MENTOR repository (for example `backend/stage6/`). Then:
```bash
chmod +x stage6 scripts/*.sh
./stage6 audit
```

Review before running `./stage6 setup`: it changes the VM and installs software from the official Docker and gVisor apt repositories. If rootless setup or cgroup delegation fails, stop; do not switch to rootful Docker or use `--ignore-cgroups` as a workaround.

**Important:** This is an infrastructure batch CLI and smoke-test harness, not the completed Stage 6 worker or a security certification. The resource script reports settings and in-container cgroup values; actual resource exhaustion tests, immutable image digest pinning, bounded output, concurrency/wall-clock enforcement, secure input validation, and worker-level timeout/cancellation/cleanup still need implementation and independent verification. No API, persistence, queue, frontend, or GitHub write integration is included.
