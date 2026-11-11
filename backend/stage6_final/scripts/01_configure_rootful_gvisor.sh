#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -ne 0 ]] || { echo "Run as normal user, not root."; exit 1; }
command -v sudo >/dev/null
sudo -v
sudo systemctl is-active --quiet docker || { echo "FAIL: rootful Docker service is not active."; exit 1; }
echo "[1/4] Installing/verifying gVisor..."
if ! command -v runsc >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor --yes -o /etc/apt/keyrings/gvisor.gpg
  sudo chmod a+r /etc/apt/keyrings/gvisor.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/gvisor.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y runsc
fi
runsc --version
echo "[2/4] Backing up and merging rootful Docker configuration..."
sudo install -m 0755 -d /etc/docker
if sudo test -f /etc/docker/daemon.json; then
  sudo cp -a /etc/docker/daemon.json "/etc/docker/daemon.json.backup.$(date -u +%Y%m%dT%H%M%SZ)"
fi
sudo python3 - <<'PY'
import json, os, tempfile
path = "/etc/docker/daemon.json"
try:
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}
if not isinstance(config, dict):
    raise SystemExit("FAIL: /etc/docker/daemon.json must contain a JSON object")
runtimes = config.setdefault("runtimes", {})
if not isinstance(runtimes, dict):
    raise SystemExit("FAIL: Docker runtimes setting is not a JSON object")
runtimes["runsc"] = {"path": "/usr/bin/runsc"}
fd, tmp = tempfile.mkstemp(prefix="daemon.json.", dir="/etc/docker")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
echo "[3/4] Restarting rootful Docker..."
sudo systemctl restart docker
for i in $(seq 1 30); do
  if sudo docker info >/dev/null 2>&1; then break; fi
  sleep 1
done
sudo docker info >/dev/null
echo "[4/4] Confirming runtime registration..."
sudo docker info --format '{{json .Runtimes}}' | grep -q '"runsc"' || { echo "FAIL: runsc is not registered with rootful Docker."; exit 1; }
echo "PASS: rootful Docker recognizes runsc."
echo "NOTE: registration is not proof that gVisor starts or enforces limits."
