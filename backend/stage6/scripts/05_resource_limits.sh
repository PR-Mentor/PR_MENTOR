#!/usr/bin/env bash
set -Eeuo pipefail
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
N="stage6-limits-$$"; trap 'docker rm -f "$N" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$N" --runtime=runsc --network=none --read-only --user=10001:10001 --memory=128m --cpus=0.5 --pids-limit=32 --tmpfs /tmp:rw,noexec,nosuid,size=16m --entrypoint python pr-mentor-stage6-executor:0.1.0 -c 'import time; time.sleep(60)' >/dev/null
docker inspect "$N" --format 'Memory={{.HostConfig.Memory}} NanoCPUs={{.HostConfig.NanoCpus}} PidsLimit={{.HostConfig.PidsLimit}} Runtime={{.HostConfig.Runtime}}'
docker exec "$N" sh -c 'cat /sys/fs/cgroup/memory.max; cat /sys/fs/cgroup/cpu.max; cat /sys/fs/cgroup/pids.max'
echo 'NOTE: compare observed cgroup values with requested limits; this is not a stress/exhaustion test.'
