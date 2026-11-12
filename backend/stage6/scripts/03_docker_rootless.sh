#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -ne 0 ]] || { echo 'Run as normal user, not root.'; exit 1; }
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras
dockerd-rootless-setuptool.sh install
systemctl --user enable docker
systemctl --user restart docker
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
docker info --format '{{json .SecurityOptions}}' | grep -q rootless || { echo 'FAIL: daemon is not rootless'; exit 1; }
echo 'PASS: rootless Docker is responding.'
