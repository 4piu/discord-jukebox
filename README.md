# Jukebox Discord Music Bot

A Discord music bot.

100% vibe coding, 0% effort. 

## Features

- **Modern Slash Commands** - All commands use Discord's `/` syntax with auto-complete
- Play music from YouTube, SoundCloud, and other supported platforms
- Advanced queue system with flexible positioning
- Playlist support
- Full playback controls (play, pause, resume, stop, skip)
- Volume control
- Queue management (view, clear, move, remove)
- Immediate playback with `playnow` command

## Quick Start

1. **Start the bot**: Run `./start.sh` or `python jukebox.py`
2. **Join a voice channel** in Discord
3. **Type `/play`** and Discord will show the command with auto-complete
4. **Enter a song name or URL** 
5. **The bot joins automatically** and starts playing!

## Available Commands

All commands use Discord's modern slash command system (`/`):

### 🎵 **Music Control**
- `/play <song>` - Play or queue a song
- `/playnext <song>` - Add song to play next in queue
- `/playnow <song>` - Skip current song and play immediately
- `/skip` - Skip current song
- `/stop` - Stop and clear queue
- `/pause` / `/resume` - Pause/resume playback

### 📋 **Queue Management**
- `/queue` - Show current queue with position numbers
- `/move <from> <to>` - Move songs between positions
- `/remove <position>` - Remove song from specific position
- `/shuffle` - Randomly shuffle the current queue
- `/clear` - Clear entire queue

### 🔧 **Bot Control**
- `/join` - Join your voice channel
- `/leave` - Leave voice channel
- `/volume <0-100>` - Set playback volume
- `/nowplaying` - Show current song info

## Queue Priority System

- **`/play`** - Adds to end of queue (normal requests)
- **`/playnext`** - Adds to front of queue (higher priority)
- **`/playnow`** - Plays immediately (urgent/emergency)

## Supported Sources

The bot supports any source that yt-dlp can handle, including:
- YouTube
- SoundCloud
- Spotify (requires additional setup)
- Bandcamp
- And many more

## Authentication with Cookies

For workaround CAPTCHA or accessing private playlists, you can provide a cookies file:

1. **Export cookies** from your browser using a browser extension (like "Get cookies.txt")
2. **Save the file** as `cookies.txt` 
3. **Set the environment variable**: `COOKIES_FILE=/path/to/cookies.txt` (path inside container)
4. **With Docker**: Mount the file into the container (see Docker section below)

**Note**: Keep your cookies file secure and don't share it, as it contains your authentication information.

**See also: [yt-dlp Wiki: Extractors](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)

## Permissions Required

Your bot needs the following Discord permissions:

__OAuth2__:
- bot

__Bot__:
- Connect
- Speak
- Send Messages
- Read Message History

## Notes

- The bot will automatically join your voice channel when you use the `/play` command
- Configurable plylist item limit (default 50)
- The bot requires ffmpeg to be installed on the system

# Docker Deployment Guide

## Container Registry

The Discord Jukebox bot is automatically built and published to GitHub Container Registry (GHCR) when releases are created.

**Image URL:** `ghcr.io/4piu/discord-jukebox`

## Quick Start with Docker

### Option 1: Docker Run
```bash
docker run -d \
  --name discord-jukebox \
  --restart unless-stopped \
  -e TOKEN=your_discord_bot_token_here \
  -e PLAYLIST_LIMIT=50 \
  ghcr.io/4piu/discord-jukebox:latest
```

**With cookies file:**
```bash
docker run -d \
  --name discord-jukebox \
  --restart unless-stopped \
  -e TOKEN=your_discord_bot_token_here \
  -e PLAYLIST_LIMIT=50 \
  -e COOKIES_FILE=/app/cookies.txt \
  -v /path/to/your/cookies.txt:/app/cookies.txt:ro \
  ghcr.io/4piu/discord-jukebox:latest
```

### Option 2: Docker Compose
1. Copy the `docker-compose.yml` file to your deployment directory
2. Create a `.env` file with your configuration:
   ```env
   DISCORD_TOKEN=your_discord_bot_token_here
   PLAYLIST_LIMIT=50
   # Optional: For authentication with cookies
   # COOKIES_FILE=/path/to/your/cookies.txt
   ```
3. **If using cookies**: Uncomment and modify the volume mount in `docker-compose.yml`:
   ```yaml
   volumes:
     - ./temp:/tmp
     - ./cookies.txt:/app/cookies.txt:ro  # Mount your cookies file
   ```
   Then set `COOKIES_FILE=/app/cookies.txt` in your `.env` file
4. Start the container:
   ```bash
   docker compose up -d
   ```

## Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `TOKEN` | Discord bot token | - | Yes |
| `PLAYLIST_LIMIT` | Maximum songs in playlist (-1 for unlimited) | 50 | No |
| `COOKIES_FILE` | Path to cookies file for yt-dlp authentication | - | No |

## Container Features

- **Multi-platform**: Built for both `linux/amd64` and `linux/arm64`
- **Security**: Runs as non-root user (uid: 1001)
- **Dependencies**: Includes FFmpeg for audio processing
- **Health checks**: Built-in container health monitoring
- **Resource efficient**: Optimized image size with slim Python base

## Updating

To update to the latest version:

```bash
# Using docker compose
docker compose pull
docker compose up -d

# Using docker run
docker stop discord-jukebox
docker rm discord-jukebox
docker pull ghcr.io/4piu/discord-jukebox:latest
# Then run the docker run command again
```

## Monitoring

Check container status:
```bash
docker ps
docker logs discord-jukebox
```

## Troubleshooting

### Common Issues

1. **Bot not connecting**: Verify your Discord token is correct
2. **Audio not playing**: Ensure the bot has proper voice channel permissions
3. **Container exits**: Check logs with `docker logs discord-jukebox`

### Getting Shell Access
```bash
docker exec -it discord-jukebox /bin/bash
```

