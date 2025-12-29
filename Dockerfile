# Build stage - Generate requirements.txt from Poetry
FROM python:3.14-slim AS builder

# Set working directory
WORKDIR /app

# Install poetry and the export plugin
RUN pip install poetry poetry-plugin-export

# Copy poetry files
COPY pyproject.toml poetry.lock* ./

# Configure poetry and export requirements
RUN poetry config virtualenvs.create false && \
    poetry export -f requirements.txt --output requirements.txt --without-hashes

# Production stage
FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for audio processing and yt-dlp
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt from builder stage
COPY --from=builder /app/requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

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
