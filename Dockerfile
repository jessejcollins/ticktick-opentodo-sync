# syntax=docker/dockerfile:1
# Multi-arch: builds natively on the Pi (linux/arm64) and on amd64 for dev.
#   docker build --target test .        # ruff + mypy + pytest
#   docker compose up -d --build        # runtime
ARG PYTHON_IMAGE=python:3.13-slim-trixie

FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install .

FROM build AS dev
RUN /opt/venv/bin/pip install '.[dev]'
ENV PATH=/opt/venv/bin:$PATH

FROM dev AS test
COPY tests ./tests
RUN /opt/venv/bin/ruff check src tests \
 && /opt/venv/bin/ruff format --check src tests \
 && /opt/venv/bin/mypy \
 && /opt/venv/bin/pytest -q -p no:cacheprovider

FROM ${PYTHON_IMAGE} AS runtime
ARG UID=10001
RUN groupadd --system --gid ${UID} ticktick-opentodo-sync \
 && useradd --system --uid ${UID} --gid ticktick-opentodo-sync --no-create-home --home-dir /data \
      --shell /usr/sbin/nologin ticktick-opentodo-sync \
 && mkdir -p /data /run/ticktick-opentodo-sync \
 && chown ticktick-opentodo-sync:ticktick-opentodo-sync /data /run/ticktick-opentodo-sync
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TICKTICK_OPENTODO_SYNC_STATE_DIR=/data \
    TICKTICK_OPENTODO_SYNC_RUN_DIR=/run/ticktick-opentodo-sync
WORKDIR /data
VOLUME /data
USER ticktick-opentodo-sync
HEALTHCHECK --interval=2m --timeout=15s --start-period=3m --retries=2 CMD ["ticktick-opentodo-sync", "--health"]
ENTRYPOINT ["ticktick-opentodo-sync"]
CMD ["--loop"]
