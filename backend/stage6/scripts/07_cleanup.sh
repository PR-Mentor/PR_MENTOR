#!/usr/bin/env bash
set -Eeuo pipefail
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
N="stage6-cleanup-$$"; trap 'docker rm -f "$N" >/dev/null 2>&1 || true' EXIT
docker run -d --name "$N" --runtime=runsc --network=none --read-only --user=10001:10001 --memory=128m --cpus=0.5 --pids-limit=32 --tmpfs /tmp:rw,noexec,nosuid,size=16m --entrypoint python pr-mentor-stage6-executor:0.1.0 -c 'import time; time.sleep(90)' >/dev/null
sleep 2
docker stop --time 1 "$N" >/dev/null
[[ "$(docker inspect "$N" --format '{{.State.Status}}')" == exited ]] || { echo 'FAIL: container did not exit'; exit 1; }
docker rm "$N" >/dev/null
! docker inspect "$N" >/dev/null 2>&1 || { echo 'FAIL: container remains'; exit 1; }
echo 'PASS: disposable container stop/remove lifecycle works; production worker timeout/cancel remains to be implemented.'
