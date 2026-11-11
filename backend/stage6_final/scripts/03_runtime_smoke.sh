#!/usr/bin/env bash
set -Eeuo pipefail
IMAGE="pr-mentor-stage6-executor:0.1.0"
sudo docker image inspect "$IMAGE" >/dev/null
echo "Running a real container with runsc..."
sudo docker run --rm \
  --runtime=runsc \
  --network=none \
  --read-only \
  --user=10001:10001 \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --pids-limit=64 \
  --memory=128m --memory-swap=128m --cpus=0.5 \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  "$IMAGE" -c 'import os, pathlib; assert os.getuid()==10001; pathlib.Path("/tmp/stage6-ok").write_text("ok"); print("PASS: runsc container started as uid", os.getuid())'
echo "PASS: real gVisor container launch."
