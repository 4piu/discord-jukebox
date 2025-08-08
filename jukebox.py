import asyncio
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
logging.basicConfig(level=logging.INFO)

# Bot configuration
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("No token found in environment variables")

# Playlist configuration
PLAYLIST_LIMIT = int(
    os.getenv("PLAYLIST_LIMIT", "50")
)  # Default to 10, -1 means no limit

# Cookie file configuration
COOKIE_FILE = os.getenv("COOKIE_FILE")
if COOKIE_FILE:
    if not os.path.exists(COOKIE_FILE):
        logging.warning(f"Cookie file {COOKIE_FILE} not found, ignoring...")
        COOKIE_FILE = None
    else:
        logging.info(f"Using cookie file: {COOKIE_FILE}")

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
if COOKIE_FILE:
    ytdl_format_options["cookiefile"] = COOKIE_FILE
    ytdl_audio_options["cookiefile"] = COOKIE_FILE

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
    "executable": "/usr/bin/ffmpeg",  # Explicitly specify FFmpeg path
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)  # For fast metadata extraction
ytdl_audio = yt_dlp.YoutubeDL(ytdl_audio_options)  # For audio URL extraction


async def get_audio_source(url):
    """Extract audio URL for streaming"""
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: ytdl_audio.extract_info(url, download=False)
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
    async def from_url(cls, url, guild_id, *, loop=None, stream=False):
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
    loop = asyncio.get_event_loop()

    # For URLs, use flat extraction
    data = await loop.run_in_executor(
        None, lambda: ytdl.extract_info(query, download=False)
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
        added_count = 0
        for entry in entries:
            song_info = {
                "url": entry["url"],
                "title": entry["title"],
                "duration": entry["duration"],
                "uploader": entry["uploader"],
                "requester": interaction.user,
            }
            queue.add(song_info, "end")
            added_count += 1

        # Send response based on single vs playlist
        await interaction.followup.send(
            f"✅ Added to queue: **{entries[0]['title']}**"
            if is_single_video
            else f"✅ Added {added_count} songs from playlist to queue"
            + (f" (limited from {total_count} total)" if was_limited else "")
        )

        # Start playing if not already playing
        if not queue.is_playing:
            await play_next(interaction)

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
        first_entry = entries[0]
        song_info = {
            "url": first_entry["url"],
            "title": first_entry["title"],
            "duration": first_entry["duration"],
            "uploader": first_entry["uploader"],
            "requester": interaction.user,
        }

        # Add remaining songs to front of queue (if playlist)
        if len(entries) > 1:
            remaining_entries = entries[1:]
            for entry in reversed(remaining_entries):
                playlist_song = {
                    "url": entry["url"],
                    "title": entry["title"],
                    "duration": entry["duration"],
                    "uploader": entry["uploader"],
                    "requester": interaction.user,
                }
                queue.add(playlist_song, "next")

            await interaction.followup.send(
                f"🎵 Playing now: **{song_info['title']}**\n✅ Added {len(remaining_entries)} more songs from playlist to queue"
                + (f" (limited from {total_count} total songs)" if was_limited else "")
            )
        else:
            await interaction.followup.send(f"🎵 Playing now: **{song_info['title']}**")

        # Stop current song if playing
        if (
            interaction.guild.voice_client
            and interaction.guild.voice_client.is_playing()
        ):
            interaction.guild.voice_client.stop()

        # Reset error count when manually starting playback
        queue.reset_error_count()
        
        # Set as current and play immediately
        queue.current = song_info
        queue.is_playing = True

        player = await YTDLSource.from_url(song_info["url"], interaction.guild.id, loop=bot.loop, stream=True)

        def after_playing(error):
            queue = get_queue(interaction.guild.id)
            if error:
                logging.error(f"Player error: {error}")
                queue.increment_error_count()
            else:
                queue.reset_error_count()

            # Use a mock context for compatibility with existing play_next function
            class MockCtx:
                def __init__(self, guild, voice_client, channel):
                    self.guild = guild
                    self.voice_client = voice_client
                    self.channel = channel

                async def send(self, content=None, embed=None):
                    if self.channel:
                        await self.channel.send(content=content, embed=embed)

            ctx_mock = MockCtx(
                interaction.guild, interaction.guild.voice_client, interaction.channel
            )
            asyncio.run_coroutine_threadsafe(
                play_next_auto(interaction.guild.id, interaction.channel), bot.loop
            )

        interaction.guild.voice_client.play(player, after=after_playing)

        duration_str = (
            f"{int(song_info['duration'])//60}:{int(song_info['duration'])%60:02d}"
            if song_info["duration"]
            else "Unknown"
        )
        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"**{song_info['title']}**",
            color=0xFF4500,  # Orange color to distinguish from regular play
        )
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(
            name="Requested by", value=song_info["requester"].mention, inline=True
        )
        embed.set_footer(text="▶️ Playing immediately (skipped queue)")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logging.error(f"Error in playnow command: {e}", exc_info=True)
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")


# Helper function for slash commands
async def play_next(interaction):
    queue = get_queue(interaction.guild.id)

    if not queue.queue:
        queue.is_playing = False
        await interaction.followup.send("📭 Queue is empty!")
        return

    # Reset error count when manually starting playback
    queue.reset_error_count()
    queue.is_playing = True
    song_info = queue.get_next()
    queue.current = song_info

    try:
        player = await YTDLSource.from_url(song_info["url"], interaction.guild.id, loop=bot.loop, stream=True)

        def after_playing(error):
            queue = get_queue(interaction.guild.id)
            if error:
                logging.error(f"Player error: {error}")
                queue.increment_error_count()
            else:
                queue.reset_error_count()

            # Use the simplified auto-play function
            asyncio.run_coroutine_threadsafe(
                play_next_auto(interaction.guild.id, interaction.channel), bot.loop
            )

        interaction.guild.voice_client.play(player, after=after_playing)

        duration_str = (
            f"{int(song_info['duration'])//60}:{int(song_info['duration'])%60:02d}"
            if song_info["duration"]
            else "Unknown"
        )
        embed = discord.Embed(
            title="Now Playing", description=f"**{song_info['title']}**", color=0x00FF00
        )
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(
            name="Requested by", value=song_info["requester"].mention, inline=True
        )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logging.error(f"Error playing song: {e}", exc_info=True)
        await interaction.followup.send(f"❌ Error playing song: {str(e)}")
        queue.is_playing = False
        await play_next(interaction)


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
        added_count = 0
        for entry in reversed(entries):
            song_info = {
                "url": entry["url"],
                "title": entry["title"],
                "duration": entry["duration"],
                "uploader": entry["uploader"],
                "requester": interaction.user,
            }
            queue.add(song_info, "next")
            added_count += 1

        await interaction.followup.send(
            f"📃 Added to queue: **{entries[0]['title']}**"
            if is_single_video
            else f"📃 Added {added_count} songs from playlist to queue"
            + (f" (limited from {total_count} total)" if was_limited else "")
        )

        # Start playing if not already playing
        if not queue.is_playing:
            await play_next(interaction)

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
            duration_str = (
                f"({int(song['duration'])//60}:{int(song['duration'])%60:02d})"
                if song["duration"]
                else ""
            )
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

    song = queue.current
    duration_str = (
        f"{int(song['duration'])//60}:{int(song['duration'])%60:02d}"
        if song["duration"]
        else "Unknown"
    )

    embed = discord.Embed(
        title="🎵 Now Playing", description=f"**{song['title']}**", color=0x00FF00
    )
    embed.add_field(name="Duration", value=duration_str, inline=True)
    embed.add_field(name="Requested by", value=song["requester"].mention, inline=True)

    await interaction.response.send_message(embed=embed)


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


# Simplified play_next function for slash commands only
async def play_next_auto(guild_id, channel):
    """Automatically play next song after current one finishes"""
    queue = get_queue(guild_id)

    # Check if we've hit the maximum consecutive errors
    if queue.get_error_count() >= MAX_PLAYBACK_ERRORS:
        queue.is_playing = False
        queue.reset_error_count()
        if channel:
            embed = discord.Embed(
                title="❌ Playback Stopped",
                description=f"Stopped after {MAX_PLAYBACK_ERRORS} consecutive playback errors. Use `/play` to try again.",
                color=0xFF0000
            )
            await channel.send(embed=embed)
        logging.warning(f"Stopped playback in guild {guild_id} after {MAX_PLAYBACK_ERRORS} consecutive errors")
        return

    if not queue.queue:
        queue.is_playing = False
        return

    queue.is_playing = True
    song_info = queue.get_next()
    queue.current = song_info

    try:
        # Get voice client from guild
        guild = bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return

        player = await YTDLSource.from_url(song_info["url"], guild_id, loop=bot.loop, stream=True)

        def after_playing(error):
            queue = get_queue(guild_id)
            if error:
                logging.error(f"Player error: {error}")
                queue.increment_error_count()
            else:
                queue.reset_error_count()

            # Schedule the next song
            asyncio.run_coroutine_threadsafe(
                play_next_auto(guild_id, channel), bot.loop
            )

        guild.voice_client.play(player, after=after_playing)

        duration_str = (
            f"{int(song_info['duration'])//60}:{int(song_info['duration'])%60:02d}"
            if song_info["duration"]
            else "Unknown"
        )
        embed = discord.Embed(
            title="Now Playing", description=f"**{song_info['title']}**", color=0x00FF00
        )
        embed.add_field(name="Duration", value=duration_str, inline=True)
        embed.add_field(
            name="Requested by", value=song_info["requester"].mention, inline=True
        )

        if channel:
            await channel.send(embed=embed)

    except Exception as e:
        logging.error(f"Error in auto play next: {e}")
        queue.increment_error_count()
        queue.is_playing = False
        await play_next_auto(guild_id, channel)


if __name__ == "__main__":
    bot.run(TOKEN)
