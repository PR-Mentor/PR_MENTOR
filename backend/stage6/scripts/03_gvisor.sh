#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -ne 0 ]] || { echo 'Run as normal user, not root.'; exit 1; }
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor --yes -o /etc/apt/keyrings/gvisor.gpg
sudo chmod a+r /etc/apt/keyrings/gvisor.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/gvisor.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list >/dev/null
sudo apt-get update
sudo apt-get install -y runsc
runsc --version
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
mkdir -p "$HOME/.config/docker"
CFG="$HOME/.config/docker/daemon.json"
if [[ -f "$CFG" ]]; then
  cp -a "$CFG" "$CFG.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  jq '.runtimes = ((.runtimes // {}) + {"runsc":{"path":"/usr/bin/runsc"}})' "$CFG" > "$CFG.tmp"
else
  printf '%s\n' '{"runtimes":{"runsc":{"path":"/usr/bin/runsc"}}}' > "$CFG.tmp"
fi
mv "$CFG.tmp" "$CFG"
systemctl --user restart docker
docker info --format '{{json .Runtimes}}' | grep -q runsc || { echo 'FAIL: runsc not registered'; exit 1; }
echo 'PASS: runsc registered. This is not proof of isolation or cgroup enforcement.'
