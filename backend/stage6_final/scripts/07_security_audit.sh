#!/usr/bin/env bash
set -Eeuo pipefail
echo "=== Stage 6 host/runtime security audit ==="
[[ "$(sudo docker info --format '{{.CgroupVersion}}')" == "2" ]] || { echo "FAIL: cgroups v2 required."; exit 1; }
driver="$(sudo docker info --format '{{.CgroupDriver}}')"
[[ "$driver" == "systemd" ]] || { echo "FAIL: expected systemd cgroup driver, got $driver"; exit 1; }
sudo docker info --format '{{json .Runtimes}}' | grep -q '"runsc"' || { echo "FAIL: runsc missing."; exit 1; }
runsc --version
echo "Cgroup driver: $driver"
echo "Cgroup version: $(sudo docker info --format '{{.CgroupVersion}}')"
echo "Docker root: $(sudo docker info --format '{{.DockerRootDir}}')"
echo "Runtime: $(sudo docker info --format '{{json .Runtimes}}')"
echo
echo "PASS: host prerequisites and runtime registration."
echo "IMPORTANT: inspect actual resource-test results above. This audit does not certify worker-level input validation, output bounds, concurrency, wall-clock orchestration, or application integration."
