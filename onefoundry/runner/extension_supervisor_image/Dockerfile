# Build context: onefoundry/. Supply an exact digest for the official Docker CLI image.
ARG DOCKER_CLI_IMAGE
FROM ${DOCKER_CLI_IMAGE} AS docker_cli

FROM python:3.12.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/extension-supervisor
COPY pyproject.toml uv.lock .python-version ./
COPY sdk/python ./sdk/python
RUN uv sync --locked --no-dev --no-install-project \
    && groupadd --gid 10001 supervisor \
    && useradd --uid 10001 --gid supervisor --home-dir /nonexistent --no-create-home supervisor
COPY runner/__init__.py runner/extension_worker_host.py runner/extension_worker_supervisor.py ./runner/

ENV PATH="/opt/extension-supervisor/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="/opt/extension-supervisor" HOME="/tmp"
USER 10001:10001
ENTRYPOINT ["python", "-m", "runner.extension_worker_supervisor"]
