#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -ne 0 ]] || { echo 'Run as normal user, not root.'; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo "Expected Ubuntu 24.04; found $PRETTY_NAME"; exit 1; }
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg uidmap dbus-user-session slirp4netns fuse-overlayfs iptables jq python3
sudo loginctl enable-linger "$USER"
echo 'PASS: prerequisites installed; user lingering enabled.'
