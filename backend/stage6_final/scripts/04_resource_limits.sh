#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="pr-mentor-stage6-executor:0.1.0"
PID_LIMIT=64
PID_ATTEMPTS=128

echo "=== Stage 6 resource enforcement verification ==="

sudo docker image inspect "$IMAGE" >/dev/null

echo "[1/3] Checking Docker-applied resource limits..."
name="stage6-limits-inspect-$RANDOM"
cleanup() {
  sudo docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sudo docker create --name "$name" --runtime=runsc --network=none \
  --memory=128m --memory-swap=128m --cpus=0.5 \
  --pids-limit="$PID_LIMIT" "$IMAGE" -c 'pass' >/dev/null

readback="$(sudo docker inspect "$name" --format \
  '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}} {{.HostConfig.Runtime}}')"
cleanup
trap - EXIT

expected="134217728 134217728 500000000 ${PID_LIMIT} runsc"
[[ "$readback" == "$expected" ]] || {
  echo "FAIL: unexpected resource configuration: $readback"
  exit 1
}
echo "PASS: Docker configuration: $readback"

echo "[2/3] Testing bounded process creation under gVisor..."
name="stage6-pids-$RANDOM"
sudo docker create --name "$name" --runtime=runsc --network=none \
  --memory=128m --memory-swap=128m --cpus=0.5 \
  --pids-limit="$PID_LIMIT" "$IMAGE" -c "
import errno, os, time
children = []
blocked_by_limit = False
try:
    for _ in range(${PID_ATTEMPTS}):
        try:
            pid = os.fork()
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                blocked_by_limit = True
                break
            raise
        if pid == 0:
            time.sleep(20)
            os._exit(0)
        children.append(pid)
finally:
    for pid in children:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    for pid in children:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

print('children_created=', len(children), 'blocked_by_EAGAIN=', blocked_by_limit)
if not blocked_by_limit:
    raise SystemExit('FAIL: process creation was not stopped by a resource limit')
if len(children) >= ${PID_ATTEMPTS}:
    raise SystemExit('FAIL: all requested children were created')
print('PASS: process creation was bounded by the configured runtime limit')
" >/dev/null

set +e
sudo docker start -a "$name"
pid_rc=$?
set -e
pid_state="$(sudo docker inspect "$name" --format '{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}')"
sudo docker rm -f "$name" >/dev/null
if [[ $pid_rc -ne 0 ]]; then
  echo "FAIL: PID workload did not complete normally (docker start exit=$pid_rc; state=$pid_state)."
  echo "A gVisor startup/runtime failure is NOT counted as a PID-limit pass."
  exit 1
fi
echo "PASS: PID-limit workload completed successfully."

echo "[3/3] Testing guest memory limit..."
name="stage6-memory-$RANDOM"
sudo docker create --name "$name" --runtime=runsc --network=none \
  --memory=96m --memory-swap=96m --cpus=0.5 "$IMAGE" -c '
try:
    block = bytearray(256 * 1024 * 1024)
except MemoryError:
    print("PASS: guest rejected allocation above configured memory budget")
else:
    raise SystemExit("FAIL: oversized allocation succeeded")
' >/dev/null

set +e
memory_output="$(sudo docker start -a "$name" 2>&1)"
mem_rc=$?
set -e
state="$(sudo docker inspect "$name" --format '{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}')"
sudo docker rm -f "$name" >/dev/null

if [[ "$memory_output" == *"PASS: guest rejected allocation above configured memory budget"* && $mem_rc -eq 0 ]]; then
  echo "$memory_output"
  echo "PASS: guest memory allocation was rejected under the configured memory budget."
elif [[ "$state" == *"|true|"* ]]; then
  echo "PASS: container was OOM-killed under the configured memory budget (state=$state)."
else
  echo "FAIL: memory test did not produce verifiable memory-limit evidence."
  echo "docker exit=$mem_rc; state=$state; output=$memory_output"
  exit 1
fi

echo "=== PASS: resource enforcement checks completed ==="
