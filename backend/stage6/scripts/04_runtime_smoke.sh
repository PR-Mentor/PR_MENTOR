#!/usr/bin/env bash
set -Eeuo pipefail
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
docker info --format '{{json .SecurityOptions}}' | grep -q rootless || { echo 'FAIL: Docker not rootless'; exit 1; }
docker info --format '{{json .Runtimes}}' | grep -q runsc || { echo 'FAIL: runsc unavailable'; exit 1; }
docker run --rm --runtime=runsc --network=none --read-only --user=10001:10001 --tmpfs /tmp:rw,noexec,nosuid,size=16m --memory=128m --cpus=0.5 --pids-limit=32 pr-mentor-stage6-executor:0.1.0 --version
echo 'PASS: disposable runsc container launched with restrictions. Enforcement still needs behavioral testing.'
