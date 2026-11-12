#!/usr/bin/env bash
set -Eeuo pipefail
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
if docker run --rm --runtime=runsc --network=none --read-only --user=10001:10001 --tmpfs /tmp:rw,noexec,nosuid,size=16m --entrypoint python pr-mentor-stage6-executor:0.1.0 -c 'import socket; socket.create_connection(("1.1.1.1",53),timeout=2)'; then echo 'FAIL: outbound network worked'; exit 1; else echo 'PASS: outbound connection blocked'; fi
if docker run --rm --runtime=runsc --network=none --read-only --user=10001:10001 --tmpfs /tmp:rw,noexec,nosuid,size=16m --entrypoint python pr-mentor-stage6-executor:0.1.0 -c 'open("/root-write-probe","w").write("x")'; then echo 'FAIL: root filesystem writable'; exit 1; else echo 'PASS: root filesystem write blocked'; fi
