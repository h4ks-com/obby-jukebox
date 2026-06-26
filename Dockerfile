FROM python:3.14-slim

# ffmpeg + libav headers for PyAV/aiortc; build tools in case a wheel must compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libavdevice-dev libavfilter-dev libavformat-dev libavcodec-dev \
        libswscale-dev libswresample-dev libavutil-dev \
        libopus-dev libvpx-dev \
        build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
ENTRYPOINT ["obby-jukebox"]
