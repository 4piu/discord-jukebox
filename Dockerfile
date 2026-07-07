# Build stage - Install dependencies with uv
FROM python:3.14-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Copy dependency files and install into a project-local venv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

# Production stage
FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for audio processing and yt-dlp
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy the application code
COPY jukebox.py ./

# Create a non-root user for security
RUN adduser --disabled-password --gecos '' --uid 1001 botuser && \
    chown -R botuser:botuser /app
USER botuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Run the bot
CMD ["python", "jukebox.py"]
