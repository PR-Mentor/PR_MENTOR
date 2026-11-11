#!/usr/bin/env bash
set -Eeuo pipefail
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
docker info --format 'SecurityOptions={{json .SecurityOptions}} CgroupDriver={{.CgroupDriver}} CgroupVersion={{.CgroupVersion}} Runtimes={{json .Runtimes}}'
docker image inspect pr-mentor-stage6-executor:0.1.0 --format 'ID={{.Id}} User={{.Config.User}}'
echo 'MANUAL GATES: verify enforced cgroup limits; no host mounts/secrets/network; immutable image digest pinning; bounded output; concurrency/wall-clock controls; safe input/path validation; cleanup after worker crash.'
echo 'This is a checklist, not a security certification.'
