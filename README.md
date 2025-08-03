# Jukebox Discord Music Bot

A Discord music bot.

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

## Permissions Required

Your bot needs the following Discord permissions:
- Connect
- Speak
- Use Voice Activity
- Send Messages
- Read Message History

## Notes

- The bot will automatically join your voice channel when you use the `/play` command
- Configurable plylist item limit (default 50)
- The bot requires ffmpeg to be installed on the system
- Songs are streamed in real-time, not downloaded
