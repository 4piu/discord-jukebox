#!/bin/bash

# Jukebox Discord Bot Startup Script

echo "Starting Jukebox Discord Music Bot..."

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "Error: .env file not found!"
    echo "Please create a .env file with your Discord bot token"
    exit 1
fi

# Check if uv is available
if ! command -v uv &> /dev/null; then
    echo "Error: uv not found!"
    echo "Please install uv: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

# Check if ffmpeg is available
if ! command -v ffmpeg &> /dev/null; then
    echo "Warning: ffmpeg not found in PATH"
    echo "Make sure ffmpeg is installed for audio processing"
fi

# Check if yt-dlp is available
if ! command -v yt-dlp &> /dev/null; then
    echo "Warning: yt-dlp not found in PATH"
    echo "Make sure yt-dlp is installed for video/audio downloading"
fi

# Check if Opus is available (required for voice playback on macOS)
if [[ "$OSTYPE" == "darwin"* ]]; then
    if [ ! -f "/opt/homebrew/opt/opus/lib/libopus.dylib" ] && [ ! -f "/usr/local/opt/opus/lib/libopus.dylib" ]; then
        echo "Warning: Opus library not found"
        echo "Install it with: brew install opus"
    fi
fi

# Sync dependencies and run the bot
echo "Syncing dependencies..."
uv sync --locked

echo "Starting bot..."
uv run python jukebox.py
