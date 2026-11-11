# Build with --build-arg PYTHON_BASE_IMAGE=<trusted-python-image>@sha256:<digest>.
# The deployed image itself must also be addressed by its resulting sha256 digest.
ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}

ARG PYTHON_VERSION=3.12.10
ARG PYTEST_VERSION=8.4.2
ARG PYTHON_BASE_IMAGE
LABEL org.prmentor.python-version="${PYTHON_VERSION}" \
      org.prmentor.pytest-version="${PYTEST_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /input
COPY sandbox/requirements.lock /opt/prmentor-requirements.lock
COPY sandbox/runner.py /opt/prmentor/runner.py
RUN python -c 'import sys; assert ".".join(map(str, sys.version_info[:3])) == "3.12.10"' \
    && PYTHON_BASE_IMAGE="${PYTHON_BASE_IMAGE}" python -c 'import os, re; assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", os.environ["PYTHON_BASE_IMAGE"])' \
    && python -m pip install --no-cache-dir --no-compile -r /opt/prmentor-requirements.lock \
    && python -c 'import pytest; assert pytest.__version__ == "8.4.2"' \
    && python -m pip uninstall --yes pip

USER 65532:65532
