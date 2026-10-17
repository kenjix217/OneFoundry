# Build context: onefoundry/. The host pins and verifies the resulting image ID.
FROM python:3.12.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/extension
COPY pyproject.toml uv.lock .python-version ./
COPY sdk/python ./sdk/python
RUN uv sync --locked --no-dev --no-install-project && \
    useradd --uid 1000 --create-home --shell /usr/sbin/nologin worker
COPY runner/extension_worker_child.py /opt/extension_worker_child.py
RUN chmod 0444 /opt/extension_worker_child.py

USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["/opt/extension/.venv/bin/python", "/opt/extension_worker_child.py"]
