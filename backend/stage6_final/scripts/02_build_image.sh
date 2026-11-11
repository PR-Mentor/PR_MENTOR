#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -ne 0 ]] || { echo "Run as normal user, not root."; exit 1; }
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="pr-mentor-stage6-executor:0.1.0"
sudo docker build --pull=false --tag "$IMAGE" --file "$ROOT/Dockerfile" "$ROOT"
sudo docker image inspect "$IMAGE" --format 'PASS: image={{.RepoTags}} id={{.Id}}'
