import asyncio
import sys
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import os
from dotenv import load_dotenv
from collections import deque
import logging

# Load environment variables
load_dotenv()

# Configure logging
LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
_LEVEL_MAP = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}
LOG_LEVEL = _LEVEL_MAP.get(LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(level=LOG_LEVEL)
if LOG_LEVEL_NAME not in _LEVEL_MAP:
    logging.warning("Invalid LOG_LEVEL '%s'; defaulting to INFO", LOG_LEVEL_NAME)

# Bot configuration
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("No token found in environment variables")

# Playlist configuration
PLAYLIST_LIMIT = int(
    os.getenv("PLAYLIST_LIMIT", "50")
)  # Default to 50, -1 means no limit

# Cookie file configuration
COOKIES_FILE = os.getenv("COOKIES_FILE")
if COOKIES_FILE:
    if not os.path.exists(COOKIES_FILE):
        logging.warning(f"Cookie file {COOKIES_FILE} not found, ignoring...")
        COOKIES_FILE = None
    else:
        logging.info(f"Using cookie file: {COOKIES_FILE}")

MAX_PLAYBACK_ERRORS = 3  # Max playback errors before stopping

# Discord bot setup - Slash commands only
intents = discord.Intents.default()
# intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)  # Set a prefix to avoid catching all messages

# yt-dlp configuration
ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": False,
    "extract_flat": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

# Separate yt-dlp instance for audio extraction (when actually playing)
ytdl_audio_options = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "extractaudio": True,
    "audioformat": "best",
    "nocheckcertificate": True,
}

# Add cookies to audio options as well
if COOKIES_FILE:
    ytdl_format_options["cookiefile"] = COOKIES_FILE
    ytdl_audio_options["cookiefile"] = COOKIES_FILE

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
    "executable": "ffmpeg",
}

if LOG_LEVEL <= logging.DEBUG:
    # Enable verbose output when debugging
    ytdl_format_options.update(
        {
            "quiet": False,
            "no_warnings": False,
            "logtostderr": True,
            "verbose": True,
        }
    )
    ytdl_audio_options.update(
        {
            "quiet": False,
            "no_warnings": False,
            "logtostderr": True,
            "verbose": True,
        }
    )
    ffmpeg_options["before_options"] += " -loglevel verbose"


def new_metadata_extractor():
    """Build a fresh YoutubeDL for fast/flat metadata extraction.

    A new instance (with its own copy of the options dict) is built per call
    rather than sharing one globally: YoutubeDL mutates its params dict and
    caches per-instance extractor state on construction, which isn't safe to
    share across the concurrent worker threads extraction runs on.
    """
    return yt_dlp.YoutubeDL(dict(ytdl_format_options))


def new_audio_extractor():
    """Build a fresh YoutubeDL for resolving a playable audio URL"""
    return yt_dlp.YoutubeDL(dict(ytdl_audio_options))


async def get_audio_source(url):
    """Extract audio URL for streaming"""
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: new_audio_extractor().extract_info(url, download=False)
        )
        if "entries" in data:
            data = data["entries"][0]
        return discord.FFmpegPCMAudio(data["url"], **ffmpeg_options)
    except Exception as e:
        raise Exception(f"Error extracting audio from URL: {e}")


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, volume=0.5):
        super().__init__(source, volume)

    @classmethod
    async def from_url(cls, url, guild_id):
        source = await get_audio_source(url)
        queue = get_queue(guild_id)
        return cls(source, volume=queue.get_volume())


class MusicQueue:
    def __init__(self):
        self.queue = deque()
        self.current = None
        self.is_playing = False
        self.volume = 0.5  # Default volume (50%)
        self.consecutive_errors = 0  # Track consecutive playback errors
        self.generation = 0  # Bumped each time playback is (re)started; lets a
        # stale after_playing callback from a superseded song detect that it
        # should not advance the queue itself
        self.notify_mode = "mute"  # "on", "mute", or "off" - auto-advance announcements

    def add(self, song_data, position="end"):
        """Add a song to the queue

        Args:
            song_data: Song information dictionary
            position: 'end' to add to end of queue, 'next' to add to beginning
        """
        if position == "next":
            self.queue.appendleft(song_data)
        else:
            self.queue.append(song_data)

    def get_next(self):
        if self.queue:
            return self.queue.popleft()
        return None

    def clear(self):
        self.queue.clear()

    def get_queue_list(self):
        return list(self.queue)

    def shuffle(self):
        """Shuffle the queue"""
        import random
        queue_list = list(self.queue)
        random.shuffle(queue_list)
        self.queue = deque(queue_list)

    def set_volume(self, volume):
        """Set volume (0.0 to 1.0)"""
        self.volume = volume

    def get_volume(self):
        """Get current volume (0.0 to 1.0)"""
        return self.volume

    def reset_error_count(self):
        """Reset consecutive error count (called on successful playback)"""
        self.consecutive_errors = 0

    def increment_error_count(self):
        """Increment consecutive error count"""
        self.consecutive_errors += 1

    def get_error_count(self):
        """Get current consecutive error count"""
        return self.consecutive_errors


# Dictionary to store music queues for each guild
music_queues = {}


def get_queue(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = MusicQueue()
    return music_queues[guild_id]


async def ensure_guild(interaction: discord.Interaction) -> bool:
    """
    Ensures the command is used in a guild (not DMs).

    Args:
        interaction: The Discord interaction

    Returns:
        bool: True if in a guild, False if in DMs

    Note:
        This function handles its own error response
    """
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ This command can only be used in a server, not in DMs!", ephemeral=True
        )
        return False
    return True


async def ensure_voice(interaction: discord.Interaction) -> bool:
    """
    Ensures the bot is connected to a voice channel for music commands.
    Assumes guild check has already been done.

    Args:
        interaction: The Discord interaction

    Returns:
        bool: True if connection is successful/already exists, False otherwise

    Note:
        This function handles its own error responses for voice-related issues
    """
    # Check if user is in a voice channel
    if not interaction.user.voice:
        await interaction.response.send_message(
            "🔊 You need to be in a voice channel to use this command!", ephemeral=True
        )
        return False

    # Connect to voice channel if not already connected
    if not interaction.guild.voice_client:
        channel = interaction.user.voice.channel
        try:
            await channel.connect()
            logging.info(f"Successfully connected to voice channel: {channel}")
            return True
        except Exception as e:
            logging.error(
                f"Failed to connect to voice channel {channel}: {e}", exc_info=True
            )
            await interaction.response.send_message(
                f"❌ Failed to join voice channel: {str(e)}", ephemeral=True
            )
            return False

    return True


def limit_playlist_entries(entries):
    """
    Get playlist entries respecting the PLAYLIST_LIMIT configuration.

    Args:
        entries: List of playlist entries

    Returns:
        tuple: (limited_entries, total_count, was_limited)
    """
    total_count = len(entries)

    if PLAYLIST_LIMIT == -1:
        # No limit
        return entries, total_count, False
    else:
        # Apply limit
        limited_entries = entries[:PLAYLIST_LIMIT]
        was_limited = total_count > PLAYLIST_LIMIT
        return limited_entries, total_count, was_limited


async def extract_playlist(query):
    """
    Fast playlist extraction using flat extraction.

    Returns:
        tuple: (playlist_entries, total_count, was_limited, is_single_video)
    """
    loop = asyncio.get_running_loop()

    # For URLs, use flat extraction
    data = await loop.run_in_executor(
        None, lambda: new_metadata_extractor().extract_info(query, download=False)
    )

    if "entries" not in data:
        # Single video - convert to standard format (early return)
        single_entry = {
            "title": data.get("title", "Unknown"),
            "duration": data.get("duration", 0),
            "uploader": data.get("uploader", "Unknown"),
            "id": data.get("id"),
            "url": data.get("webpage_url"),
        }
        return [single_entry], 1, False, True

    # Apply playlist limit to flat entries
    entries, total_count, was_limited = limit_playlist_entries(data["entries"])

    if not entries:
        return [], 0, False, False

    # Convert flat entries to standardized format
    processed_entries = []
    for entry in entries:
        processed_entry = {
            "title": entry.get("title", "Unknown"),
            "duration": entry.get("duration", 0),
            "uploader": entry.get("uploader", "Unknown"),
            "id": entry.get("id"),
            "url": entry.get("url"),
        }
        processed_entries.append(processed_entry)

    return processed_entries, total_count, was_limited, False


def format_duration(duration):
    """Format a duration in seconds as M:SS, or 'Unknown' if not available"""
    if not duration:
        return "Unknown"
    duration = int(duration)
    return f"{duration // 60}:{duration % 60:02d}"


def build_song_info(entry, requester):
    """Build the song dict stored in the queue from an extracted entry"""
    return {
        "url": entry["url"],
        "title": entry["title"],
        "duration": entry["duration"],
        "uploader": entry["uploader"],
        "requester": requester,
    }


def build_now_playing_embed(song_info, *, title="🎵 Now Playing", color=0x00FF00, footer=None):
    embed = discord.Embed(title=title, description=f"**{song_info['title']}**", color=color)
    embed.add_field(name="Duration", value=format_duration(song_info["duration"]), inline=True)
    embed.add_field(name="Requested by", value=song_info["requester"].mention, inline=True)
    if footer:
        embed.set_footer(text=footer)
    return embed


async def play_song(guild_id, channel, song_info):
    """Start playing song_info right now, superseding any current playback.

    Returns True if playback started, False if the audio couldn't be extracted
    or the bot isn't connected to voice anymore.
    """
    queue = get_queue(guild_id)
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return False

    try:
        player = await YTDLSource.from_url(song_info["url"], guild_id)
    except Exception as e:
        logging.error(f"Error extracting audio for playback: {e}", exc_info=True)
        return False

    # Bump the generation before touching the voice client so a stale
    # after_playing callback from whatever was playing before (fired by the
    # stop() call below) can tell it's been superseded and must not advance
    # the queue itself.
    queue.generation += 1
    generation = queue.generation
    queue.current = song_info
    queue.is_playing = True

    def after_playing(error):
        if error:
            logging.error(f"Player error: {error}")
        if generation != queue.generation:
            return  # a newer playback has already superseded this one
        if error:
            queue.increment_error_count()
        else:
            queue.reset_error_count()
        asyncio.run_coroutine_threadsafe(advance_queue(guild_id, channel), bot.loop)

    voice_client = guild.voice_client
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
    voice_client.play(player, after=after_playing)
    return True


async def send_notification(channel, queue, *, embed):
    """Send an automatic (not user-command-triggered) playback announcement,
    honoring the guild's notify_mode: 'on' sends normally, 'mute' sends
    without pinging anyone, 'off' skips it entirely."""
    if not channel or queue.notify_mode == "off":
        return
    await channel.send(embed=embed, silent=(queue.notify_mode == "mute"))


async def advance_queue(guild_id, channel):
    """Play the next queued song, or stop if the queue is empty or too many
    consecutive errors have piled up"""
    queue = get_queue(guild_id)

    if queue.get_error_count() >= MAX_PLAYBACK_ERRORS:
        queue.is_playing = False
        queue.reset_error_count()
        embed = discord.Embed(
            title="❌ Playback Stopped",
            description=f"Stopped after {MAX_PLAYBACK_ERRORS} consecutive playback errors. Use `/play` to try again.",
            color=0xFF0000,
        )
        await send_notification(channel, queue, embed=embed)
        logging.warning(f"Stopped playback in guild {guild_id} after {MAX_PLAYBACK_ERRORS} consecutive errors")
        return

    if not queue.queue:
        queue.is_playing = False
        return

    song_info = queue.get_next()

    if await play_song(guild_id, channel, song_info):
        queue.reset_error_count()
        await send_notification(channel, queue, embed=build_now_playing_embed(song_info))
    else:
        queue.increment_error_count()
        await advance_queue(guild_id, channel)


# Sync slash commands on ready
@bot.event
async def on_ready():
    logging.info(f"{bot.user} has connected to Discord!")
    logging.info(f"Bot is in {len(bot.guilds)} guilds")

    try:
        synced = await bot.tree.sync()
        logging.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}")


@bot.tree.command(name="play", description="Play a song from URL or search term")
async def cmd_play(interaction: discord.Interaction, query: str):
    # Check guild first, then voice connection
    if not await ensure_guild(interaction):
        return
    if not await ensure_voice(interaction):
        return

    await interaction.response.defer()

    queue = get_queue(interaction.guild.id)

    try:
        # Use fast flat extraction for everything
        entries, total_count, was_limited, is_single_video = await extract_playlist(
            query
        )

        if not entries:
            await interaction.followup.send("❌ No songs found!")
            return

        # Add all songs to queue
        for entry in entries:
            queue.add(build_song_info(entry, interaction.user), "end")

        # Send response based on single vs playlist
        await interaction.followup.send(
            f"✅ Added to queue: **{entries[0]['title']}**"
            if is_single_video
            else f"✅ Added {len(entries)} songs from playlist to queue"
            + (f" (limited from {total_count} total)" if was_limited else "")
        )

        # Start playing if not already playing
        if not queue.is_playing:
            await advance_queue(interaction.guild.id, interaction.channel)

    except Exception as e:
        logging.error(f"Error in play command: {e}", exc_info=True)
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")


@bot.tree.command(
    name="playnow",
    description="Skip current song and play the specified song immediately",
)
async def cmd_playnow(interaction: discord.Interaction, query: str):
    # Check guild first, then voice connection
    if not await ensure_guild(interaction):
        return
    if not await ensure_voice(interaction):
        return

    await interaction.response.defer()

    queue = get_queue(interaction.guild.id)

    try:
        # Use fast flat extraction
        entries, total_count, was_limited, is_single_video = await extract_playlist(
            query
        )

        if not entries:
            await interaction.followup.send("❌ No songs found!")
            return

        # Take first song for immediate play
        song_info = build_song_info(entries[0], interaction.user)

        # Add remaining songs to front of queue (if playlist)
        remaining_entries = entries[1:]
        for entry in reversed(remaining_entries):
            queue.add(build_song_info(entry, interaction.user), "next")

        success = await play_song(interaction.guild.id, interaction.channel, song_info)
        if not success:
            await interaction.followup.send(f"❌ Failed to play **{song_info['title']}**")
            return
        queue.reset_error_count()

        embed = build_now_playing_embed(
            song_info,
            color=0xFF4500,  # Orange color to distinguish from regular play
            footer="▶️ Playing immediately (skipped queue)",
        )
        if remaining_entries:
            embed.add_field(
                name="📃 Queue Updated",
                value=f"Added {len(remaining_entries)} more song(s) from playlist"
                + (f" (limited from {total_count} total songs)" if was_limited else ""),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logging.error(f"Error in playnow command: {e}", exc_info=True)
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")


# Add missing slash commands to complete the functionality
@bot.tree.command(name="playnext", description="Add a song to play next in the queue")
async def cmd_playnext(interaction: discord.Interaction, query: str):
    # Check guild first, then voice connection
    if not await ensure_guild(interaction):
        return
    if not await ensure_voice(interaction):
        return

    await interaction.response.defer()

    queue = get_queue(interaction.guild.id)

    try:
        # Use fast flat extraction
        entries, total_count, was_limited, is_single_video = await extract_playlist(
            query
        )

        if not entries:
            await interaction.followup.send("❌ No songs found!")
            return

        # Add all songs to front of queue in reverse order so first song plays first
        for entry in reversed(entries):
            queue.add(build_song_info(entry, interaction.user), "next")

        await interaction.followup.send(
            f"📃 Added to queue: **{entries[0]['title']}**"
            if is_single_video
            else f"📃 Added {len(entries)} songs from playlist to queue"
            + (f" (limited from {total_count} total)" if was_limited else "")
        )

        # Start playing if not already playing
        if not queue.is_playing:
            await advance_queue(interaction.guild.id, interaction.channel)

    except Exception as e:
        logging.error(f"Error in playnext command: {e}", exc_info=True)
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")


@bot.tree.command(name="queue", description="Show the current music queue")
async def cmd_queue(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue_list = queue.get_queue_list()

    if not queue_list and not queue.current:
        await interaction.response.send_message("📭 Queue is empty!", ephemeral=True)
        return

    embed = discord.Embed(title="Music Queue", color=0x0099FF)

    if queue.current:
        embed.add_field(
            name="🎵 Now Playing",
            value=f"**{queue.current['title']}**\nRequested by {queue.current['requester'].mention}",
            inline=False,
        )

    if queue_list:
        queue_text = ""
        for i, song in enumerate(queue_list[:10], 1):  # Show first 10 songs
            duration_str = f"({format_duration(song['duration'])})" if song["duration"] else ""
            queue_text += f"**{i}.** **{song['title']}** {duration_str}\n   Requested by {song['requester'].mention}\n\n"

        embed.add_field(name="📃 Up Next", value=queue_text[:1024], inline=False)

        if len(queue_list) > 10:
            embed.add_field(
                name="...", value=f"And {len(queue_list) - 10} more songs", inline=False
            )

        embed.set_footer(text="Use /move and /remove to manage the queue")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="skip", description="Skip the current song")
async def cmd_skip(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped!")
    else:
        await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)


@bot.tree.command(name="join", description="Join your voice channel")
async def cmd_join(interaction: discord.Interaction):
    # Check guild first, then voice connection
    if not await ensure_guild(interaction):
        return
    if not await ensure_voice(interaction):
        return

    # If we get here, we're successfully connected
    channel = interaction.user.voice.channel

    # Check if we moved to a different channel
    if interaction.guild.voice_client.channel != channel:
        try:
            await interaction.guild.voice_client.move_to(channel)
            await interaction.response.send_message(f"🎧 Moved to {channel}")
        except Exception as e:
            logging.error(
                f"Failed to move to voice channel {channel}: {e}", exc_info=True
            )
            await interaction.response.send_message(
                f"❌ Failed to move to voice channel: {str(e)}", ephemeral=True
            )
    else:
        await interaction.response.send_message(f"🎧 Already connected to {channel}")


@bot.tree.command(name="leave", description="Leave the voice channel")
async def cmd_leave(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client:
        queue = get_queue(interaction.guild.id)
        queue.clear()
        queue.is_playing = False
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 Left the voice channel")
    else:
        await interaction.response.send_message(
            "❌ I'm not in a voice channel!", ephemeral=True
        )


@bot.tree.command(name="stop", description="Stop playing and clear the queue")
async def cmd_stop(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client:
        queue = get_queue(interaction.guild.id)
        queue.clear()
        queue.is_playing = False
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("⏹️ Stopped playing and cleared queue!")
    else:
        await interaction.response.send_message("❌ Not playing anything!", ephemeral=True)


@bot.tree.command(name="pause", description="Pause the current song")
async def pause_slash(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.pause()
        await interaction.response.send_message("⏸️ Paused!")
    else:
        await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song")
async def cmd_resume(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client and interaction.guild.voice_client.is_paused():
        interaction.guild.voice_client.resume()
        await interaction.response.send_message("▶️ Resumed!")
    else:
        await interaction.response.send_message("❌ Nothing is paused!", ephemeral=True)


@bot.tree.command(name="volume", description="Change the volume (0-100)")
async def cmd_volume(interaction: discord.Interaction, volume: int):
    if not await ensure_guild(interaction):
        return

    if not interaction.guild.voice_client:
        await interaction.response.send_message(
            "❌ Not connected to a voice channel!", ephemeral=True
        )
        return

    if not 0 <= volume <= 100:
        await interaction.response.send_message(
            "❌ Volume must be between 0 and 100!", ephemeral=True
        )
        return

    # Convert percentage to decimal and store in queue
    volume_decimal = volume / 100
    queue = get_queue(interaction.guild.id)
    queue.set_volume(volume_decimal)

    # Apply to current playing song if any
    if interaction.guild.voice_client.source:
        interaction.guild.voice_client.source.volume = volume_decimal
        await interaction.response.send_message(f"🔊 Volume set to {volume}% (current song updated)")
    else:
        await interaction.response.send_message(f"🔊 Volume set to {volume}% (will apply to next song)")


@bot.tree.command(name="nowplaying", description="Show the currently playing song")
async def cmd_nowplaying(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)

    if not queue.current:
        await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
        return

    embed = build_now_playing_embed(queue.current)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="notifications",
    description="Control the automatic now-playing announcements for this server",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="On - announce every song normally", value="on"),
        app_commands.Choice(name="Mute - announce, but without pinging anyone", value="mute"),
        app_commands.Choice(name="Off - don't announce automatically", value="off"),
    ]
)
async def cmd_notifications(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue.notify_mode = mode.value
    await interaction.response.send_message(f"🔔 Notifications set to **{mode.name}**", ephemeral=True)


@bot.tree.command(name="clear", description="Clear the entire queue")
async def cmd_clear(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue.clear()
    await interaction.response.send_message("🗑️ Queue cleared!")


@bot.tree.command(name="shuffle", description="Shuffle the current queue")
async def cmd_shuffle(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue_list = queue.get_queue_list()

    if not queue_list:
        await interaction.response.send_message("📭 Queue is empty! Nothing to shuffle.", ephemeral=True)
        return

    if len(queue_list) == 1:
        await interaction.response.send_message("📭 Only one song in queue! Nothing to shuffle.", ephemeral=True)
        return

    # Shuffle the queue
    queue.shuffle()
    await interaction.response.send_message(f"🔀 Shuffled {len(queue_list)} songs in the queue!")


@bot.tree.command(name="move", description="Move a song in the queue")
async def cmd_move(
    interaction: discord.Interaction, from_position: int, to_position: int
):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue_list = queue.get_queue_list()

    if not queue_list:
        await interaction.response.send_message("📭 Queue is empty!", ephemeral=True)
        return

    # Convert to 0-based indexing
    from_index = from_position - 1
    to_index = to_position - 1

    if not (0 <= from_index < len(queue_list)) or not (0 <= to_index < len(queue_list)):
        await interaction.response.send_message(
            f"❌ Invalid position! Queue has {len(queue_list)} songs (1-{len(queue_list)})",
            ephemeral=True,
        )
        return

    if from_index == to_index:
        await interaction.response.send_message(
            "❌ Source and destination positions are the same!", ephemeral=True
        )
        return

    # Remove and re-insert the song
    song = queue_list.pop(from_index)
    queue_list.insert(to_index, song)

    # Update the queue
    queue.queue = deque(queue_list)

    await interaction.response.send_message(
        f"✅ Moved **{song['title']}** from position {from_position} to position {to_position}"
    )


@bot.tree.command(name="remove", description="Remove a song from the queue")
async def cmd_remove(interaction: discord.Interaction, position: int):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue_list = queue.get_queue_list()

    if not queue_list:
        await interaction.response.send_message("📭 Queue is empty!", ephemeral=True)
        return

    # Convert to 0-based indexing
    index = position - 1

    if not (0 <= index < len(queue_list)):
        await interaction.response.send_message(
            f"❌ Invalid position! Queue has {len(queue_list)} songs (1-{len(queue_list)})",
            ephemeral=True,
        )
        return

    # Remove the song
    removed_song = queue_list.pop(index)

    # Update the queue
    queue.queue = deque(queue_list)

    await interaction.response.send_message(
        f"❌ Removed **{removed_song['title']}** from position {position}"
    )


def load_opus():
    """Load Opus library on macOS if not already loaded"""
    if discord.opus.is_loaded():
        return

    if sys.platform.startswith("darwin"):
        brew_lib_path = "/opt/homebrew/opt/opus/lib"
        if os.path.isdir(brew_lib_path):
            current = os.environ.get("DYLD_LIBRARY_PATH", "")
            paths = current.split(os.pathsep) if current else []
            if brew_lib_path not in paths:
                paths.append(brew_lib_path)
                os.environ["DYLD_LIBRARY_PATH"] = os.pathsep.join(paths)
                logging.debug(f"DYLD_LIBRARY_PATH={os.environ['DYLD_LIBRARY_PATH']}")
        else:
            logging.warning(f"Expected Homebrew opus path missing: {brew_lib_path}")
        discord.opus.load_opus("libopus.dylib")
    else:
        logging.info("Skip manual loading Opus library on this platform.")


if __name__ == "__main__":
    load_opus()
    bot.run(TOKEN)
