#!/bin/bash

# Jukebox Discord Bot Startup Script

echo "Starting Jukebox Discord Music Bot..."

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "Error: .env file not found!"
    echo "Please create a .env file with your Discord bot token"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Error: Virtual environment not found!"
    echo "Please set up the virtual environment first"
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

# Activate virtual environment and run the bot
echo "Activating virtual environment..."
source .venv/bin/activate

echo "Starting bot..."
python jukebox.py
