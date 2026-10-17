FROM python:3.12.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev
COPY src ./src
COPY runner/video_edit_render.py ./runner/video_edit_render.py
ENV GPG_KEY=""
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH="/app/src:/app" PYTHONDONTWRITEBYTECODE=1
LABEL onefoundry.video_edit.contract="1"
ENTRYPOINT ["python", "-m", "runner.video_edit_render"]
