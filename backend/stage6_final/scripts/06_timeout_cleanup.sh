#!/usr/bin/env bash
set -Eeuo pipefail
IMAGE="pr-mentor-stage6-executor:0.1.0"
name="stage6-cleanup-$RANDOM"
echo "Starting disposable long-running gVisor container..."
sudo docker run -d --name "$name" --runtime=runsc --network=none "$IMAGE" -c 'import time; time.sleep(60)' >/dev/null
echo "Stopping it with a bounded grace period..."
sudo docker stop --time 1 "$name" >/dev/null
sudo docker rm "$name" >/dev/null
if sudo docker inspect "$name" >/dev/null 2>&1; then
  echo "FAIL: container metadata remains after removal."
  exit 1
fi
echo "PASS: stop/remove lifecycle cleaned up the disposable container."
echo "NOTE: this tests Docker lifecycle cleanup, not yet the future worker's wall-clock timeout implementation."
