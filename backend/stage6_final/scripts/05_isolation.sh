#!/usr/bin/env bash
set -Eeuo pipefail
IMAGE="pr-mentor-stage6-executor:0.1.0"
echo "[1/3] Checking runtime and container security configuration..."
name="stage6-isolation-$RANDOM"
sudo docker create --name "$name" --runtime=runsc --network=none --read-only \
  --user=10001:10001 --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=64 --memory=128m --memory-swap=128m --cpus=0.5 \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m "$IMAGE" -c 'pass' >/dev/null
cfg="$(sudo docker inspect "$name" --format '{{.HostConfig.Runtime}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.ReadonlyRootfs}}|{{.Config.User}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}')"
sudo docker rm "$name" >/dev/null
expected='runsc|none|true|10001:10001|["ALL"]|["no-new-privileges"]'
[[ "$cfg" == "$expected" ]] || { echo "FAIL: security configuration mismatch: $cfg"; exit 1; }
echo "PASS: runtime, network-none, read-only root, non-root user, dropped capabilities and no-new-privileges."
echo "[2/3] Checking read-only root filesystem and writable bounded scratch..."
sudo docker run --rm --runtime=runsc --network=none --read-only --user=10001:10001 \
  --tmpfs /tmp:rw,noexec,nosuid,size=8m "$IMAGE" -c '
import pathlib
pathlib.Path("/tmp/write-test").write_text("ok")
try:
    pathlib.Path("/workspace/should-not-write").write_text("bad")
except OSError:
    print("PASS: scratch writable; image root filesystem read-only")
else:
    raise SystemExit("FAIL: wrote to read-only workspace")
'
echo "[3/3] Checking network isolation..."
sudo docker run --rm --runtime=runsc --network=none "$IMAGE" -c '
import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=2)
except OSError:
    print("PASS: external network connection unavailable")
else:
    raise SystemExit("FAIL: container reached external network")
'
