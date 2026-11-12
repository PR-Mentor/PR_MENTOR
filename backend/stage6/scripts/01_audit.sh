#!/usr/bin/env bash
set -Eeuo pipefail
echo '=== OS ==='; cat /etc/os-release
echo '=== KERNEL / VIRTUALIZATION ==='; uname -r; systemd-detect-virt || true
echo '=== CPU / MEMORY / DISK ==='; nproc; free -h; df -h /
echo '=== CGROUPS ==='; stat -fc %T /sys/fs/cgroup; cat /sys/fs/cgroup/cgroup.controllers || true
echo '=== DOCKER ==='; docker info 2>&1 || true
echo '=== RUNSC ==='; runsc --version 2>&1 || true
