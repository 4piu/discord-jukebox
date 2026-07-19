import asyncio
import queue as thread_queue
import re
import shlex
import signal
import sys
import threading
from urllib.parse import urlsplit
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
ffmpeg_logger = logging.getLogger("jukebox.ffmpeg")
# Colourised output when the stream supports it (TTY, or Docker where `docker
# logs` is normally viewed on one); plain text otherwise. NO_COLOR forces
# plain output, e.g. when shipping container logs to a file/collector.
if os.environ.get("NO_COLOR"):
    discord.utils.setup_logging(
        level=LOG_LEVEL,
        formatter=logging.Formatter(
            "[{asctime}] [{levelname:<8}] {name}: {message}",
            "%Y-%m-%d %H:%M:%S",
            style="{",
        ),
    )
else:
    discord.utils.setup_logging(level=LOG_LEVEL)
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

# Max consecutive playback errors before stopping
MAX_PLAYBACK_ERRORS = int(os.getenv("MAX_PLAYBACK_ERRORS", "3"))

# Loop mode each guild starts with (and /stop resets to): "off", "song", or "queue"
DEFAULT_LOOP_MODE = os.getenv("DEFAULT_LOOP_MODE", "queue").lower()
if DEFAULT_LOOP_MODE not in ("off", "song", "queue"):
    logging.warning("Invalid DEFAULT_LOOP_MODE '%s'; defaulting to queue", DEFAULT_LOOP_MODE)
    DEFAULT_LOOP_MODE = "queue"

# Played songs remembered per voice session (for /history, /previous);
# -1 means unlimited, 0 disables history
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))
if HISTORY_LIMIT < 0:
    HISTORY_LIMIT = None  # deque(maxlen=None) = unbounded

def env_flag(name, default):
    """Boolean env var: '0', 'false', 'no', 'off' (any case) mean off"""
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def env_nonnegative_float(name, default):
    """Read a non-negative float environment variable, with a safe fallback."""
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError:
        logging.warning("Invalid %s '%s'; defaulting to %s", name, raw_value, default)
        return default
    if value < 0:
        logging.warning("Invalid %s '%s'; defaulting to %s", name, raw_value, default)
        return default
    return value


# Optional PCM read-ahead buffer. It absorbs brief input stalls (for example,
# a reconnecting CDN socket) before Discord's 20 ms voice sender underruns.
# Defaults provide a small, bounded cushion while adding one second of track
# start latency. Set AUDIO_BUFFER_SECONDS=0 to disable it explicitly.
AUDIO_BUFFER_SECONDS = env_nonnegative_float("AUDIO_BUFFER_SECONDS", 3)
AUDIO_BUFFER_STARTUP_SECONDS = env_nonnegative_float(
    "AUDIO_BUFFER_STARTUP_SECONDS", 1
)


# Command receipts ("✅ Added to queue", "⏭️ Skipped!", ...) are shown only to
# the invoker (ephemeral) to keep the channel quiet; the channel-wide signal is
# the auto-announcement system governed by /notifications. Set to false to get
# the old public replies back.
EPHEMERAL_REPLIES = env_flag("EPHEMERAL_REPLIES", "true")

# Playback control buttons (⏮️⏯️⏭️ / 🔁🔂↪️) on now-playing cards; set to
# false to send plain cards without buttons.
CONTROL_BUTTONS = env_flag("CONTROL_BUTTONS", "true")

# Discord bot setup - Slash commands only
intents = discord.Intents.default()
# intents.message_content = True
intents.voice_states = True
class JukeboxBot(commands.Bot):
    async def setup_hook(self):
        # Handle shutdown signals from inside the event loop. The default ^C
        # path (KeyboardInterrupt) cancels the gateway reader task before
        # close() runs, so the voice_state_update confirming each voice
        # disconnect is never processed and discord.py sits out its full 30s
        # confirmation timeout per voice client. Closing while the loop (and
        # gateway reader) is still running lets the confirmation arrive
        # immediately.
        self._shutdown_requested = False
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_shutdown_signal)
        # Route presses of the playback-control buttons (fixed custom_ids) to
        # a fresh view, including buttons on cards sent before a restart.
        self.add_view(JukeboxControls())

    def _on_shutdown_signal(self):
        if self._shutdown_requested:
            logging.warning("Second shutdown signal received, forcing exit.")
            os._exit(1)
        self._shutdown_requested = True
        logging.info("Shutdown signal received, closing client...")
        asyncio.create_task(self.close())

    async def close(self):
        logging.info("Client close started")
        await super().close()
        logging.info("Client close finished")


bot = JukeboxBot(command_prefix="!", intents=intents)  # Set a prefix to avoid catching all messages

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
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 "
        "-reconnect_on_network_error 1 -reconnect_delay_max 5"
    ),
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


def new_search_extractor():
    """Build a fresh YoutubeDL that fully resolves a search query (no flat
    extraction), used to follow the redirect a plain search term produces"""
    return yt_dlp.YoutubeDL(dict(ytdl_format_options, extract_flat=False))


def ffmpeg_before_options(http_headers):
    """Return FFmpeg input options, including yt-dlp's media request headers.

    Extractors commonly provide a User-Agent and other headers needed for the
    resolved CDN URL.  FFmpeg otherwise sends its own default User-Agent,
    which can result in more frequent upstream connection resets.  Validate
    header names and strip line breaks from values so extractor data cannot
    add command-line options or additional HTTP header lines unexpectedly.
    """
    before_options = ffmpeg_options["before_options"]
    header_lines = []
    for name, value in (http_headers or {}).items():
        name = str(name)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            continue
        value = str(value).replace("\r", "").replace("\n", "")
        header_lines.append(f"{name}: {value}")

    if header_lines:
        # FFmpeg expects one argument containing CRLF-separated HTTP headers.
        before_options += " -headers " + shlex.quote("\r\n".join(header_lines) + "\r\n")
    return before_options


class FFmpegStderrLogger:
    """File-like sink that gives FFmpeg stderr lines normal application logs.

    discord.py recognises a file-like object without ``fileno()`` and pipes
    FFmpeg's stderr to its ``write`` method in a reader thread.  That avoids
    FFmpeg writing directly to the container's stdout/stderr without the
    logging formatter (and therefore without timestamps).
    """

    def __init__(self, media_host):
        self.media_host = media_host
        self._pending = ""

    def write(self, data):
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        self._pending += text
        lines = self._pending.splitlines(keepends=True)
        self._pending = ""
        for line in lines:
            if line.endswith(("\n", "\r")):
                ffmpeg_logger.warning("[%s] %s", self.media_host, line.rstrip())
            else:
                self._pending = line
        return len(data)

    def flush(self):
        if self._pending:
            ffmpeg_logger.warning("[%s] %s", self.media_host, self._pending)
            self._pending = ""


class TimestampedFFmpegPCMAudio(discord.FFmpegPCMAudio):
    """Read FFmpeg stderr per line rather than discord.py's 8 KiB chunks."""

    def _pipe_reader(self, dest):
        while self._process:
            if self._stderr is None:
                break
            try:
                line = self._stderr.readline()
            except Exception:
                ffmpeg_logger.exception("Unable to read FFmpeg stderr")
                dest.flush()
                return
            if not line:
                break
            dest.write(line)
        dest.flush()


class BufferedPCMAudio(discord.AudioSource):
    """Read PCM from an FFmpeg source ahead of Discord's voice send loop."""

    FRAME_BYTES = 3840  # 20 ms of 48 kHz, stereo, signed 16-bit PCM
    FRAME_SECONDS = 0.02

    def __init__(self, source, buffer_seconds, startup_seconds):
        self.source = source
        self._stop = threading.Event()
        self._eof = threading.Event()
        self._ready = threading.Event()
        self._underrun = False
        max_frames = max(1, int(buffer_seconds / self.FRAME_SECONDS))
        self._startup_frames = min(
            max_frames, int(startup_seconds / self.FRAME_SECONDS + 0.999999)
        )
        self._frames = thread_queue.Queue(maxsize=max_frames)
        self._reader = threading.Thread(
            target=self._read_ahead,
            daemon=True,
            name="jukebox-pcm-read-ahead",
        )
        self._reader.start()

    def _read_ahead(self):
        try:
            while not self._stop.is_set():
                frame = self.source.read()
                if not frame:
                    break
                while not self._stop.is_set():
                    try:
                        self._frames.put(frame, timeout=0.1)
                        break
                    except thread_queue.Full:
                        pass
                if self._frames.qsize() >= self._startup_frames:
                    self._ready.set()
        except Exception:
            logging.exception("PCM read-ahead buffer stopped unexpectedly")
        finally:
            self._eof.set()
            self._ready.set()

    def read(self):
        try:
            # AudioPlayer keeps its own 20 ms clock and catches up if read()
            # blocks. Never wait here: an empty buffer must yield silence, not
            # make Discord send subsequent frames in a burst.
            frame = self._frames.get_nowait()
        except thread_queue.Empty:
            if self._eof.is_set():
                return b""
            if not self._underrun:
                logging.warning("PCM read-ahead buffer underrun; sending silence")
                self._underrun = True
            return b"\0" * self.FRAME_BYTES
        if self._underrun:
            logging.info("PCM read-ahead buffer recovered")
            self._underrun = False
        return frame

    def wait_until_ready(self):
        """Block only before VoiceClient.play starts its 20 ms clock."""
        self._ready.wait()

    def cleanup(self):
        self._stop.set()
        self.source.cleanup()


async def get_audio_source(url):
    """Extract audio URL for streaming"""
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(
            None, lambda: new_audio_extractor().extract_info(url, download=False)
        )
        if "entries" in data:
            data = data["entries"][0]
        source_options = dict(ffmpeg_options)
        source_options["before_options"] = ffmpeg_before_options(
            data.get("http_headers")
        )
        media_host = urlsplit(data["url"]).hostname or "unknown-media-host"
        ffmpeg_logger.info("Starting FFmpeg media stream from %s", media_host)
        return TimestampedFFmpegPCMAudio(
            data["url"], stderr=FFmpegStderrLogger(media_host), **source_options
        )
    except Exception as e:
        raise Exception(f"Error extracting audio from URL: {e}")


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, volume=0.5):
        super().__init__(source, volume)

    @classmethod
    async def from_url(cls, url, guild_id):
        source = await get_audio_source(url)
        if AUDIO_BUFFER_SECONDS > 0:
            source = BufferedPCMAudio(
                source, AUDIO_BUFFER_SECONDS, AUDIO_BUFFER_STARTUP_SECONDS
            )
            if AUDIO_BUFFER_STARTUP_SECONDS > 0:
                await asyncio.to_thread(source.wait_until_ready)
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
        self.loop_mode = DEFAULT_LOOP_MODE  # "off", "song", or "queue"
        self.history = deque(maxlen=HISTORY_LIMIT)  # Songs played this voice
        # session, oldest first; cleared when the bot leaves voice
        self.skip_requested = False  # Set by /skip, consumed once by
        # advance_queue so a skip advances even under loop_mode "song"

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

    # A plain search term (not a URL/playlist) doesn't get extracted here -
    # flat extraction doesn't follow redirects, so it comes back as a shallow
    # pointer to e.g. "ytsearch:query" instead of real metadata. Resolve it
    # with one more, fully-resolving extraction; it's always a single result.
    if data.get("_type") == "url":
        search_result = await loop.run_in_executor(
            None, lambda: new_search_extractor().extract_info(data["url"], download=False)
        )
        entry = search_result["entries"][0] if "entries" in search_result else search_result
        single_entry = {
            "title": entry.get("title", "Unknown"),
            "duration": entry.get("duration", 0),
            "uploader": entry.get("uploader", "Unknown"),
            "id": entry.get("id"),
            # Use webpage_url, not the (possibly expiring, format-specific)
            # resolved stream url - it gets re-resolved fresh at play time.
            "url": entry.get("webpage_url"),
        }
        return [single_entry], 1, False, True

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


def loop_suffix(queue):
    """Loop-mode emoji for card labels, with a leading space ('' when off)"""
    return {"song": " 🔂", "queue": " 🔁"}.get(queue.loop_mode, "")


# Short confirmation labels, distinct from the longer descriptive text shown
# in each command's dropdown (app_commands.Choice.name) - a confirmation
# reply shouldn't echo back the whole picker description.
LOOP_MODE_LABELS = {
    "queue": "🔁 Loop queue",
    "song": "🔂 Loop current song",
    "off": "↪️ No loop",
}
NOTIFY_MODE_LABELS = {"on": "On", "mute": "Muted", "off": "Off"}


def build_now_playing_embed(song_info, *, label="🎵 Now Playing", color=0x00FF00, footer=None, up_next=None):
    """Compact now-playing card: small label on top, the song title as a
    clickable link to the source, one detail line, and an optional footer
    ('Up next' has to live there as plain text - footers can't hold links)."""
    embed = discord.Embed(title=song_info["title"], url=song_info.get("url"), color=color)
    embed.set_author(name=label)
    details = []
    if song_info["duration"]:
        details.append(format_duration(song_info["duration"]))
    details.append(f"requested by {song_info['requester'].mention}")
    embed.description = " · ".join(details)
    footer_parts = [footer, f"Up next: {up_next}" if up_next else None]
    footer_text = " · ".join(p for p in footer_parts if p)
    if footer_text:
        embed.set_footer(text=footer_text)
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
        asyncio.run_coroutine_threadsafe(
            advance_queue(guild_id, channel, finished=song_info, errored=bool(error)),
            bot.loop,
        )

    voice_client = guild.voice_client
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()
    voice_client.play(player, after=after_playing)
    return True


async def send_notification(channel, queue, *, embed, view=None, force=False):
    """Send an automatic (not user-command-triggered) playback announcement,
    honoring the guild's notify_mode: 'on' sends normally, 'mute' sends
    without pinging anyone, 'off' skips it entirely. force=True bypasses the
    'off' skip - used by /playnow, whose card is a deliberate manual
    announcement rather than an automatic one."""
    if not channel or (queue.notify_mode == "off" and not force):
        return
    # view is falsy when absent (None or discord.utils.MISSING)
    kwargs = {"view": view} if view else {}
    await channel.send(embed=embed, silent=(queue.notify_mode == "mute"), **kwargs)


async def advance_queue(guild_id, channel, finished=None, errored=False):
    """Play the next song after `finished` ended (or kick off playback when
    called with no `finished`, e.g. from /play on an idle queue), honoring the
    guild's loop mode. Stops if the queue is empty or too many consecutive
    errors have piled up."""
    queue = get_queue(guild_id)

    skip_requested = queue.skip_requested
    queue.skip_requested = False

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

    if finished is not None and not errored:
        # Log it, unless this is just the same song coming around again
        # (song-loop replays, or a one-song ring under queue-loop).
        if not queue.history or queue.history[-1] is not finished:
            queue.history.append(finished)
        if queue.loop_mode == "song" and not skip_requested:
            # Replay without an announcement - repeats aren't news. On
            # extraction failure fall through to normal advancement; the
            # error breaker above caps how often this can retry.
            if await play_song(guild_id, channel, finished):
                return
            queue.increment_error_count()
            await advance_queue(guild_id, channel)
            return
        if queue.loop_mode == "queue":
            queue.add(finished, "end")

    if not queue.queue:
        queue.is_playing = False
        return

    song_info = queue.get_next()

    if await play_song(guild_id, channel, song_info):
        queue.reset_error_count()
        # A ring under loop_mode "queue" can cycle back to the exact song
        # that just finished (e.g. a single-song queue) - skip the
        # announcement then too, same reasoning as the song-loop skip above:
        # nothing changed, so it isn't news.
        if song_info is not finished:
            embed = build_now_playing_embed(
                song_info,
                label=f"🎵 Now Playing{loop_suffix(queue)}",
                up_next=queue.queue[0]["title"] if queue.queue else None,
            )
            await send_notification(channel, queue, embed=embed, view=controls_view())
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


@bot.event
async def on_voice_state_update(member, before, after):
    """End the playback session when the bot leaves voice for any reason
    (/leave, kicked, dragged out and disconnected): forget what was playing
    and clear the session history. The queue itself is kept so a /join +
    /play can pick up where things left off."""
    if member.id != bot.user.id or after.channel is not None:
        return
    queue = get_queue(member.guild.id)
    queue.generation += 1  # stale-ify any in-flight after_playing callback
    queue.current = None
    queue.is_playing = False
    queue.skip_requested = False
    queue.history.clear()


INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)",
    re.IGNORECASE,
)


@bot.event
async def on_message(message: discord.Message):
    """Join a voice channel by DM'd invite link (e.g. Discord's
    "Invite to Channel" UI, which delivers the invite as a DM).

    Overriding on_message disables prefix-command processing, which this
    slash-commands-only bot doesn't use.
    """
    if message.author.bot or message.guild is not None:
        return

    match = INVITE_RE.search(message.content)
    if not match:
        await message.channel.send(
            "👋 Send me a voice channel invite link and I'll join it. "
            "Music playback is controlled with slash commands in the server."
        )
        return

    try:
        invite = await bot.fetch_invite(match.group(1))
    except discord.NotFound:
        await message.channel.send("❌ That invite is invalid or has expired.")
        return
    except discord.HTTPException as e:
        logging.error(f"Failed to resolve invite: {e}")
        await message.channel.send("❌ Couldn't resolve that invite, please try again later.")
        return

    # Resolve the partial invite channel against the bot's own cache; a miss
    # means the bot isn't in that guild.
    channel = bot.get_channel(invite.channel.id) if invite.channel else None
    if channel is None:
        await message.channel.send(
            "❌ I'm not a member of that server, so I can't join its channels."
        )
        return
    if not isinstance(channel, discord.VoiceChannel):
        await message.channel.send("❌ That invite doesn't point to a voice channel.")
        return

    # Only follow invitations from someone who is in the channel themselves -
    # otherwise anyone could summon the bot into arbitrary channels.
    if not any(m.id == message.author.id for m in channel.members):
        await message.channel.send(
            f"❌ You need to be in **{channel.name}** yourself before inviting me."
        )
        return

    perms = channel.permissions_for(channel.guild.me)
    if not (perms.connect and perms.speak):
        await message.channel.send(
            f"❌ I don't have permission to connect and speak in **{channel.name}**."
        )
        return

    voice_client = channel.guild.voice_client
    if voice_client:
        if voice_client.channel and voice_client.channel.id == channel.id:
            await message.channel.send(f"✅ I'm already in **{channel.name}**!")
        elif voice_client.is_playing() or voice_client.is_paused():
            await message.channel.send(
                f"❌ I'm busy playing music in **{voice_client.channel.name}** right now."
            )
        else:
            await voice_client.move_to(channel)
            await message.channel.send(f"✅ Moved to **{channel.name}**!")
        return

    try:
        await channel.connect()
    except Exception as e:
        logging.error(f"Failed to join {channel} via invite: {e}", exc_info=True)
        await message.channel.send(f"❌ Failed to join **{channel.name}**: {e}")
        return

    logging.info(f"Joined voice channel {channel} via DM invite from {message.author}")
    await message.channel.send(
        f"✅ Joined **{channel.name}**! Use `/play` in the server to queue music."
    )


@bot.tree.command(name="play", description="Add a song to the end of the queue")
async def cmd_play(interaction: discord.Interaction, query: str):
    # Check guild first, then voice connection
    if not await ensure_guild(interaction):
        return
    if not await ensure_voice(interaction):
        return

    await interaction.response.defer(ephemeral=EPHEMERAL_REPLIES)

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
    description="Skip the current song and play this one immediately",
)
async def cmd_playnow(interaction: discord.Interaction, query: str):
    # Check guild first, then voice connection
    if not await ensure_guild(interaction):
        return
    if not await ensure_voice(interaction):
        return

    await interaction.response.defer(ephemeral=EPHEMERAL_REPLIES)

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
            up_next=queue.queue[0]["title"] if queue.queue else None,
        )
        if remaining_entries:
            embed.add_field(
                name="📃 Queue Updated",
                value=f"Added {len(remaining_entries)} more song(s) from playlist"
                + (f" (limited from {total_count} total songs)" if was_limited else ""),
                inline=False,
            )

        # /playnow interrupts whatever was playing for everyone in the
        # channel, so - unlike other command receipts - it stays public even
        # with EPHEMERAL_REPLIES on; it doubles as the announcement. Sent
        # through send_notification (force=True to bypass notify_mode "off")
        # rather than the followup, because once the interaction was
        # deferred ephemeral, Discord locks every followup to ephemeral too.
        await send_notification(interaction.channel, queue, embed=embed, view=controls_view(), force=True)
        await interaction.followup.send("▶️ Playing now!", ephemeral=EPHEMERAL_REPLIES)

    except Exception as e:
        logging.error(f"Error in playnow command: {e}", exc_info=True)
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")


# Add missing slash commands to complete the functionality
@bot.tree.command(name="playnext", description="Add a song to the front of the queue")
async def cmd_playnext(interaction: discord.Interaction, query: str):
    # Check guild first, then voice connection
    if not await ensure_guild(interaction):
        return
    if not await ensure_voice(interaction):
        return

    await interaction.response.defer(ephemeral=EPHEMERAL_REPLIES)

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


@bot.tree.command(name="queue", description="Show the current queue")
async def cmd_queue(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue_list = queue.get_queue_list()

    if not queue_list and not queue.current:
        await interaction.response.send_message("📭 Queue is empty!", ephemeral=True)
        return

    embed = discord.Embed(title="Music Queue", color=0x0099FF)
    loop_label = {"queue": "🔁 Looping the queue", "song": "🔂 Looping the current song"}.get(
        queue.loop_mode
    )
    if loop_label:
        embed.description = loop_label

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

    await interaction.response.send_message(embed=embed, ephemeral=EPHEMERAL_REPLIES)


async def skip_impl(interaction: discord.Interaction):
    """Skip the current song - shared by /skip and the ⏭️ button"""
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        queue = get_queue(interaction.guild.id)
        # Under loop_mode "song", advance_queue would otherwise replay the
        # skipped song; this flag makes it advance once. Under "queue" the
        # skipped song still re-appends - the ring keeps turning.
        queue.skip_requested = True
        interaction.guild.voice_client.stop()
        await interaction.response.send_message("⏭️ Skipped!", ephemeral=EPHEMERAL_REPLIES)
    else:
        await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)


async def playpause_impl(interaction: discord.Interaction):
    """Toggle pause/resume - the ⏯️ button"""
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await interaction.response.send_message("⏸️ Paused!", ephemeral=EPHEMERAL_REPLIES)
    elif voice_client and voice_client.is_paused():
        voice_client.resume()
        await interaction.response.send_message("▶️ Resumed!", ephemeral=EPHEMERAL_REPLIES)
    else:
        await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)


async def set_loop_impl(interaction: discord.Interaction, mode):
    """Set the loop mode - shared by /loop and the loop-mode buttons"""
    queue = get_queue(interaction.guild.id)
    queue.loop_mode = mode
    await interaction.response.send_message(
        f"Loop mode set to **{LOOP_MODE_LABELS[mode]}**", ephemeral=EPHEMERAL_REPLIES
    )


class JukeboxControls(discord.ui.View):
    """Playback control buttons attached to now-playing cards.

    Stateless and persistent: each button acts on whatever is playing NOW in
    the guild (exactly like its slash-command counterpart), so buttons on old
    cards never go stale; the fixed custom_ids plus the add_view() call in
    setup_hook keep them working even on cards sent before a restart."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⏮️", custom_id="jukebox:previous", style=discord.ButtonStyle.secondary, row=0)
    async def on_previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await ensure_guild(interaction):
            await previous_impl(interaction)

    @discord.ui.button(emoji="⏯️", custom_id="jukebox:playpause", style=discord.ButtonStyle.secondary, row=0)
    async def on_playpause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await ensure_guild(interaction):
            await playpause_impl(interaction)

    @discord.ui.button(emoji="⏭️", custom_id="jukebox:skip", style=discord.ButtonStyle.secondary, row=0)
    async def on_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await ensure_guild(interaction):
            await skip_impl(interaction)

    @discord.ui.button(emoji="🔁", custom_id="jukebox:loop_queue", style=discord.ButtonStyle.secondary, row=1)
    async def on_loop_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await ensure_guild(interaction):
            await set_loop_impl(interaction, "queue")

    @discord.ui.button(emoji="🔂", custom_id="jukebox:loop_song", style=discord.ButtonStyle.secondary, row=1)
    async def on_loop_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await ensure_guild(interaction):
            await set_loop_impl(interaction, "song")

    @discord.ui.button(emoji="↪️", custom_id="jukebox:loop_off", style=discord.ButtonStyle.secondary, row=1)
    async def on_loop_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await ensure_guild(interaction):
            await set_loop_impl(interaction, "off")


def controls_view():
    """Playback buttons for a now-playing card, or MISSING (the send APIs'
    view-parameter default, meaning no components) when CONTROL_BUTTONS is off"""
    return JukeboxControls() if CONTROL_BUTTONS else discord.utils.MISSING


@bot.tree.command(name="skip", description="Skip the current song")
async def cmd_skip(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return
    await skip_impl(interaction)


async def previous_impl(interaction: discord.Interaction):
    """Play the previously played song - shared by /previous and the ⏮️ button"""
    queue = get_queue(interaction.guild.id)

    if not interaction.guild.voice_client:
        await interaction.response.send_message(
            "❌ I'm not in a voice channel!", ephemeral=True
        )
        return

    # Walk history back to the most recent song that isn't the one already
    # playing (identity check: a looping song logs itself once but IS current).
    target = None
    for i in range(len(queue.history) - 1, -1, -1):
        if queue.history[i] is not queue.current:
            target = queue.history[i]
            del queue.history[i]
            break

    if target is None and queue.current is None:
        await interaction.response.send_message(
            "📭 Nothing has been played yet!", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=EPHEMERAL_REPLIES)

    if target is None:
        # Empty history: restart the current song from the beginning.
        if await play_song(interaction.guild.id, interaction.channel, queue.current):
            await interaction.followup.send(
                f"⏮️ No previous song - restarting **{queue.current['title']}**"
            )
        else:
            await interaction.followup.send("❌ Failed to restart the current song")
        return

    # Under queue-loop the finished song was re-appended to the ring; pull that
    # copy (same object) back out so it doesn't come around twice.
    for i, song in enumerate(queue.queue):
        if song is target:
            del queue.queue[i]
            break

    interrupted = queue.current
    if await play_song(interaction.guild.id, interaction.channel, target):
        # Keep the interrupted song next in line, so /skip returns forward to
        # it. (Safe to add after starting: the queue isn't consumed until the
        # previous song finishes playing.)
        if interrupted:
            queue.add(interrupted, "next")
        await interaction.followup.send(f"⏮️ Playing previous: **{target['title']}**")
    else:
        await interaction.followup.send(f"❌ Failed to play **{target['title']}**")


@bot.tree.command(name="previous", description="Go back and play the previously played song")
async def cmd_previous(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return
    await previous_impl(interaction)


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
            await interaction.response.send_message(
                f"🎧 Moved to {channel}", ephemeral=EPHEMERAL_REPLIES
            )
        except Exception as e:
            logging.error(
                f"Failed to move to voice channel {channel}: {e}", exc_info=True
            )
            await interaction.response.send_message(
                f"❌ Failed to move to voice channel: {str(e)}", ephemeral=True
            )
    else:
        await interaction.response.send_message(
            f"🎧 Already connected to {channel}", ephemeral=EPHEMERAL_REPLIES
        )


@bot.tree.command(name="leave", description="Leave the voice channel")
async def cmd_leave(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client:
        queue = get_queue(interaction.guild.id)
        queue.clear()
        queue.is_playing = False
        queue.current = None
        queue.history.clear()
        # Stale-ify the pending after_playing callback now rather than waiting
        # for on_voice_state_update, so nothing tries to advance mid-disconnect.
        queue.generation += 1
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message(
            "👋 Left the voice channel", ephemeral=EPHEMERAL_REPLIES
        )
    else:
        await interaction.response.send_message(
            "❌ I'm not in a voice channel!", ephemeral=True
        )


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def cmd_stop(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client:
        queue = get_queue(interaction.guild.id)
        queue.clear()
        queue.is_playing = False
        queue.current = None
        queue.loop_mode = DEFAULT_LOOP_MODE
        # Stale-ify the pending after_playing callback so the loop mode can't
        # resurrect the stopped song. History is kept: it already played.
        queue.generation += 1
        interaction.guild.voice_client.stop()
        await interaction.response.send_message(
            "⏹️ Stopped playing and cleared queue!", ephemeral=EPHEMERAL_REPLIES
        )
    else:
        await interaction.response.send_message("❌ Not playing anything!", ephemeral=True)


@bot.tree.command(name="pause", description="Pause the current song")
async def pause_slash(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.pause()
        await interaction.response.send_message("⏸️ Paused!", ephemeral=EPHEMERAL_REPLIES)
    else:
        await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song")
async def cmd_resume(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    if interaction.guild.voice_client and interaction.guild.voice_client.is_paused():
        interaction.guild.voice_client.resume()
        await interaction.response.send_message("▶️ Resumed!", ephemeral=EPHEMERAL_REPLIES)
    else:
        await interaction.response.send_message("❌ Nothing is paused!", ephemeral=True)


@bot.tree.command(name="volume", description="Set the volume (0-100)")
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
        await interaction.response.send_message(
            f"🔊 Volume set to {volume}% (current song updated)", ephemeral=EPHEMERAL_REPLIES
        )
    else:
        await interaction.response.send_message(
            f"🔊 Volume set to {volume}% (will apply to next song)", ephemeral=EPHEMERAL_REPLIES
        )


@bot.tree.command(name="nowplaying", description="Show the currently playing song")
async def cmd_nowplaying(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)

    if not queue.current:
        await interaction.response.send_message("❌ Nothing is playing!", ephemeral=True)
        return

    embed = build_now_playing_embed(
        queue.current,
        label=f"🎵 Now Playing{loop_suffix(queue)}",
        up_next=queue.queue[0]["title"] if queue.queue else None,
    )
    await interaction.response.send_message(
        embed=embed, ephemeral=EPHEMERAL_REPLIES, view=controls_view()
    )


@bot.tree.command(
    name="notifications",
    description="Control automatic now-playing announcements",
)
@app_commands.describe(
    mode="on=every song, mute=no pinging, off=no announcements"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="mute", value="mute"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def cmd_notifications(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue.notify_mode = mode.value
    await interaction.response.send_message(
        f"🔔 Notifications set to **{NOTIFY_MODE_LABELS[mode.value]}**", ephemeral=True
    )


@bot.tree.command(name="loop", description="Set the loop mode")
@app_commands.describe(
    mode="queue=repeat queue, song=repeat one song, off=play each song once"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="queue", value="queue"),
        app_commands.Choice(name="song", value="song"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def cmd_loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    if not await ensure_guild(interaction):
        return

    await set_loop_impl(interaction, mode.value)


@bot.tree.command(name="history", description="Show songs played this session")
async def cmd_history(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)

    if not queue.history:
        await interaction.response.send_message(
            "📭 Nothing has been played yet!", ephemeral=True
        )
        return

    embed = discord.Embed(title="📜 Playback History", color=0x0099FF)
    history_text = ""
    for i, song in enumerate(reversed(list(queue.history)[-10:]), 1):  # Most recent first
        duration_str = f"({format_duration(song['duration'])})" if song["duration"] else ""
        history_text += f"**{i}.** **{song['title']}** {duration_str}\n   Requested by {song['requester'].mention}\n\n"
    embed.add_field(name="Most recent first", value=history_text[:1024], inline=False)

    if len(queue.history) > 10:
        embed.set_footer(text=f"And {len(queue.history) - 10} more this session")

    await interaction.response.send_message(embed=embed, ephemeral=EPHEMERAL_REPLIES)


@bot.tree.command(name="clear", description="Clear the queue")
async def cmd_clear(interaction: discord.Interaction):
    if not await ensure_guild(interaction):
        return

    queue = get_queue(interaction.guild.id)
    queue.clear()
    await interaction.response.send_message("🗑️ Queue cleared!", ephemeral=EPHEMERAL_REPLIES)


@bot.tree.command(name="shuffle", description="Shuffle the queue")
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
    await interaction.response.send_message(
        f"🔀 Shuffled {len(queue_list)} songs in the queue!", ephemeral=EPHEMERAL_REPLIES
    )


@bot.tree.command(name="move", description="Move a song to a different position in the queue")
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
        f"✅ Moved **{song['title']}** from position {from_position} to position {to_position}",
        ephemeral=EPHEMERAL_REPLIES,
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
        f"❌ Removed **{removed_song['title']}** from position {position}",
        ephemeral=EPHEMERAL_REPLIES,
    )


def load_opus():
    """Load Opus library on macOS if not already loaded"""
    if discord.opus.is_loaded():
        return

    if sys.platform.startswith("darwin"):
        # Homebrew's lib dir isn't on the default dylib search path, and setting
        # DYLD_LIBRARY_PATH from within the already-running process doesn't
        # retroactively affect ctypes' dlopen calls in this process - so resolve
        # the actual file and load it by absolute path instead.
        candidates = [
            "/opt/homebrew/opt/opus/lib/libopus.dylib",  # Apple Silicon Homebrew
            "/usr/local/opt/opus/lib/libopus.dylib",  # Intel Homebrew
        ]
        for path in candidates:
            if os.path.exists(path):
                discord.opus.load_opus(path)
                return
        logging.error(
            f"libopus not found (checked {candidates}). Install it with `brew install opus`."
        )
    else:
        logging.info("Skip manual loading Opus library on this platform.")


def _handle_sigterm(signum, frame):
    # As PID 1 in a container, unhandled SIGTERM is discarded by the kernel,
    # so `docker stop` would never reach us. This only covers the brief
    # startup window before the event loop runs; once JukeboxBot.setup_hook
    # installs its loop-level handlers, they supersede this one.
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)
    load_opus()
    # log_handler=None: logging is already configured via basicConfig above;
    # discord.py would otherwise install a second handler and double-print.
    bot.run(TOKEN, log_handler=None)
    # Discord cleanup (voice disconnect, websocket/http close) is done once
    # bot.run() returns. In-flight yt-dlp extractions run on non-daemon
    # executor threads that can't be cancelled and would otherwise block
    # interpreter exit until their network calls finish - skip waiting on them.
    logging.info("Shutdown complete, exiting.")
    os._exit(0)
