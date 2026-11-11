#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/image"
cat > "$ROOT/image/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 worker && python -m pip install --no-cache-dir pytest==8.3.5
USER 10001:10001
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
ENTRYPOINT ["python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]
EOF
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
docker build --pull -t pr-mentor-stage6-executor:0.1.0 "$ROOT/image"
docker image inspect pr-mentor-stage6-executor:0.1.0 --format 'Image={{.Id}} User={{.Config.User}}'
