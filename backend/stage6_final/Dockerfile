FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

RUN python -m pip install --no-cache-dir --no-compile pytest==8.3.5 \
    && groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin worker \
    && mkdir -p /workspace \
    && chown 10001:10001 /workspace

WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["python"]
