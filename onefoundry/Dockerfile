FROM node:22.22.2-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /runner-spool && chown 10001:10001 /runner-spool
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY sdk/python ./sdk/python
RUN uv sync --locked --no-dev
COPY src ./src
COPY runner ./runner
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=frontend /frontend/dist ./frontend/dist
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src:/app" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "onefoundry.main:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12.12-slim AS test
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY sdk/python ./sdk/python
RUN uv sync --locked
COPY src ./src
COPY migrations ./migrations
COPY tests ./tests
COPY runner ./runner
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src:/app" PYTHONUNBUFFERED=1

# Maintenance QA has dependencies without baked application source in its
# import path. /app is hidden by the existing networkless test boundary.
FROM test AS repository-qa
RUN UV_PROJECT_ENVIRONMENT=/opt/qa-venv uv sync --locked --no-install-project
ENV PATH="/opt/qa-venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="/workspace/onefoundry/src:/workspace/src:/workspace/onefoundry:/workspace" \
    PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS="-p no:cacheprovider"
WORKDIR /workspace

FROM python:3.12.12-slim AS runner
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 runner \
    && useradd --uid 10001 --gid runner --home-dir /runner-home --create-home runner \
    && mkdir -p /runner-spool /opt/onefoundry \
    && chown runner:runner /runner-spool /runner-home
WORKDIR /opt/onefoundry
COPY runner ./runner
USER runner:runner
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "runner.service"]

FROM runtime AS github-stage-runner
COPY runner ./runner
ENV PYTHONPATH="/app/src:/app"
USER 10001:10001
CMD ["python", "-m", "runner.github_stage_supervisor"]

FROM runtime AS browser-static-bridge
USER 10001:10001
CMD ["python", "-m", "runner.browser_static_volume_bridge"]

FROM runtime AS input-worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
CMD ["python", "-m", "onefoundry.input_file_worker"]
