import asyncio
import os
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

# 1. 設定機器人的 Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
app_commands_synced = False

# 2. 設定 yt-dlp 與 FFmpeg 的參數
ytdl_format_options = {
    # Use "best" to allow fallback to combined audio+video streams, bypassing audio-only format restrictions
    "format": "best",
    "extractor_args": {"youtube": ["player_client=android", "player_skip=webpage"]},
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "cachedir": False,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cookiefile": "cookies.txt",
}

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    # -vn: 不要影像
    # -threads 1: 限制單執行緒，大幅降低 NAS 的 CPU 瞬間負載
    "options": "-vn -threads 1",
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)
IDLE_DISCONNECT_SECONDS = 300
MAX_QUEUE_LENGTH = 100
AUTO_DELETE_USER_COMMANDS = True
DELETE_COMMAND_DELAY_SECONDS = 1.0
DELETE_ON_COMMAND_ERROR = True
DELETE_PERMISSION_NOTICE_COOLDOWN_SECONDS = 300


@dataclass
class Song:
    title: str
    webpage_url: str
    stream_url: str
    duration: Optional[int] = None


@dataclass
class GuildMusicState:
    queue: deque[Song] = field(default_factory=deque)
    now_playing: Optional[Song] = None
    text_channel_id: Optional[int] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    idle_disconnect_task: Optional[asyncio.Task] = None
    last_delete_permission_notice_at: float = 0.0


music_states: dict[int, GuildMusicState] = {}


def get_state(guild_id: int) -> GuildMusicState:
    state = music_states.get(guild_id)
    if state is None:
        state = GuildMusicState()
        music_states[guild_id] = state
    return state


def cleanup_guild_state_if_idle(guild_id: int) -> None:
    state = music_states.get(guild_id)
    if state is None:
        return

    has_pending_idle_task = state.idle_disconnect_task and not state.idle_disconnect_task.done()
    if state.queue or state.now_playing or has_pending_idle_task:
        return

    music_states.pop(guild_id, None)


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "未知"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


async def extract_song(query: str) -> Song:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    if data is None:
        raise ValueError("無法取得音訊資訊")

    if "entries" in data:
        entries = [entry for entry in data["entries"] if entry]
        if not entries:
            raise ValueError("找不到可播放的結果")
        data = entries[0]

    stream_url = data.get("url")
    title = data.get("title") or "未知標題"
    webpage_url = data.get("webpage_url") or query
    duration = data.get("duration")

    if not stream_url:
        raise ValueError("取得串流網址失敗")
    return Song(title=title, webpage_url=webpage_url, stream_url=stream_url, duration=duration)


def classify_error(error: Exception) -> str:
    if isinstance(error, yt_dlp.utils.DownloadError):
        return "無法取得 YouTube 音訊，請確認網址或稍後重試。"
    if isinstance(error, discord.ClientException):
        return "語音客戶端發生問題，請重新加入語音頻道後再試。"
    if isinstance(error, ValueError):
        return str(error)
    return f"未預期錯誤：{type(error).__name__}"


def cancel_idle_disconnect(state: GuildMusicState) -> None:
    if state.idle_disconnect_task and not state.idle_disconnect_task.done():
        state.idle_disconnect_task.cancel()
    state.idle_disconnect_task = None


async def idle_disconnect_worker(guild_id: int) -> None:
    state = get_state(guild_id)
    try:
        await asyncio.sleep(IDLE_DISCONNECT_SECONDS)

        guild = bot.get_guild(guild_id)
        if guild is None:
            return

        voice_client = guild.voice_client
        if voice_client is None:
            return

        should_disconnect = False
        async with state.lock:
            if not state.queue and not voice_client.is_playing() and not voice_client.is_paused():
                state.now_playing = None
                should_disconnect = True

        if should_disconnect:
            await voice_client.disconnect()
            if state.text_channel_id:
                channel = bot.get_channel(state.text_channel_id)
                if channel:
                    await channel.send("🛌 佇列已清空且閒置一段時間，已自動離開語音頻道。")
    except asyncio.CancelledError:
        return
    finally:
        current_task = asyncio.current_task()
        if state.idle_disconnect_task is current_task:
            state.idle_disconnect_task = None
        cleanup_guild_state_if_idle(guild_id)


def schedule_idle_disconnect(guild_id: int) -> None:
    state = get_state(guild_id)
    cancel_idle_disconnect(state)
    state.idle_disconnect_task = asyncio.create_task(idle_disconnect_worker(guild_id))


async def _notify_delete_permission_issue(ctx: commands.Context) -> None:
    if ctx.guild is None:
        return

    state = get_state(ctx.guild.id)
    now = asyncio.get_running_loop().time()
    if now - state.last_delete_permission_notice_at < DELETE_PERMISSION_NOTICE_COOLDOWN_SECONDS:
        return

    state.last_delete_permission_notice_at = now
    await ctx.send("⚠️ 我沒有刪除訊息權限，無法自動清理使用者指令訊息。")


async def cleanup_user_command_message(ctx: commands.Context, *, from_error: bool = False) -> None:
    if not AUTO_DELETE_USER_COMMANDS:
        return

    if from_error and not DELETE_ON_COMMAND_ERROR:
        return

    if getattr(ctx, "_command_message_cleaned", False):
        return

    message = ctx.message
    if message is None:
        return

    if ctx.guild is not None:
        me = ctx.guild.me
        if me is not None and not ctx.channel.permissions_for(me).manage_messages:
            await _notify_delete_permission_issue(ctx)
            return

    if DELETE_COMMAND_DELAY_SECONDS > 0:
        await asyncio.sleep(DELETE_COMMAND_DELAY_SECONDS)

    try:
        await message.delete()
        setattr(ctx, "_command_message_cleaned", True)
    except discord.NotFound:
        setattr(ctx, "_command_message_cleaned", True)
    except discord.Forbidden:
        await _notify_delete_permission_issue(ctx)
    except discord.HTTPException:
        pass


async def ensure_same_voice_channel(ctx: commands.Context) -> bool:
    if ctx.voice_client is None or ctx.voice_client.channel is None:
        await ctx.send("我目前不在語音頻道中。")
        return False

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("你必須先加入和我同一個語音頻道。")
        return False

    if ctx.author.voice.channel != ctx.voice_client.channel:
        await ctx.send(f"請到同一個語音頻道再操作：{ctx.voice_client.channel.mention}")
        return False
    return True


async def ensure_voice_for_play(ctx: commands.Context) -> bool:
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("你必須先加入一個語音頻道！")
        return False

    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
        return True

    if ctx.voice_client.channel != channel:
        await ctx.send(f"我目前在 {ctx.voice_client.channel.mention}，請到同一頻道再控制播放。")
        return False
    return True


async def create_audio_source(stream_url: str) -> discord.FFmpegOpusAudio:
    # from_probe 會在可行時使用 copy，讓 Opus 來源可直通送進 Discord。
    return await discord.FFmpegOpusAudio.from_probe(
        stream_url,
        method="fallback",
        **ffmpeg_options,
    )


async def play_next(ctx: commands.Context) -> None:
    voice_client = ctx.voice_client
    if voice_client is None:
        return
    loop = asyncio.get_running_loop()

    state = get_state(ctx.guild.id)
    async with state.lock:
        if voice_client.is_playing() or voice_client.is_paused():
            return

        if not state.queue:
            state.now_playing = None
            schedule_idle_disconnect(ctx.guild.id)
            return

        cancel_idle_disconnect(state)
        next_song = state.queue.popleft()
        state.now_playing = next_song

    try:
        source = await create_audio_source(next_song.stream_url)
    except Exception as error:
        state.now_playing = None
        await ctx.send(f"⚠️ 無法建立 Opus 音訊來源：{classify_error(error)}")
        await play_next(ctx)
        return

    async def handle_after_playback(error: Optional[Exception]) -> None:
        if error:
            print(f"播放發生錯誤: {error}")
            await ctx.send(f"⚠️ 播放中斷：{classify_error(error)}")
        await play_next(ctx)

    def after_playback(error: Optional[Exception]) -> None:
        loop.call_soon_threadsafe(lambda: asyncio.create_task(handle_after_playback(error)))

    try:
        voice_client.play(source, after=after_playback)
    except Exception as error:
        state.now_playing = None
        await ctx.send(f"⚠️ 無法開始播放：{classify_error(error)}")
        await play_next(ctx)
        return

    await ctx.send(f"🎵 現在正在播放: **{next_song.title}** ({format_duration(next_song.duration)})")


# 3. 建立指令
@bot.event
async def on_ready():
    global app_commands_synced

    if not app_commands_synced:
        try:
            synced = await bot.tree.sync()
        except discord.HTTPException as error:
            print(f"同步斜線指令失敗：{error}")
        else:
            app_commands_synced = True
            print(f"已同步 {len(synced)} 個斜線指令。")

    print(f"成功登入！機器人名稱：{bot.user}")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    state = music_states.pop(guild.id, None)
    if state is not None:
        cancel_idle_disconnect(state)


@bot.after_invoke
async def after_invoke_cleanup(ctx):
    await cleanup_user_command_message(ctx)


@bot.hybrid_command(name="join", help="讓機器人加入你的語音頻道")
@app_commands.guild_only()
async def join(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("你必須先加入一個語音頻道！")
        return

    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
    elif ctx.voice_client.channel != channel:
        state = get_state(ctx.guild.id)
        is_busy = ctx.voice_client.is_playing() or ctx.voice_client.is_paused() or bool(state.queue)
        if is_busy:
            await ctx.send(f"目前正在服務 {ctx.voice_client.channel.mention}，請到同一頻道操作。")
            return
        await ctx.voice_client.move_to(channel)

    await ctx.send(f"已加入語音頻道：{ctx.voice_client.channel}")


@bot.hybrid_command(name="play", aliases=["p"], help="播放 YouTube 音樂 (輸入: !play <網址或關鍵字>)")
@app_commands.guild_only()
@app_commands.describe(query="YouTube 網址或關鍵字")
async def play(ctx, *, query: str):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    if not await ensure_voice_for_play(ctx):
        return

    state = get_state(ctx.guild.id)
    state.text_channel_id = ctx.channel.id

    if ctx.interaction is not None:
        if not ctx.interaction.response.is_done():
            await ctx.defer()
        try:
            song = await extract_song(query)
        except Exception as error:
            print(f"[play] extract_song error: {error}")
            await ctx.send(f"⚠️ 播放失敗：{classify_error(error)}")
            return
    else:
        async with ctx.typing():
            try:
                song = await extract_song(query)
            except Exception as error:
                print(f"[play] extract_song error: {error}")
                await ctx.send(f"⚠️ 播放失敗：{classify_error(error)}")
                return

    queue_limit_reached = False
    async with state.lock:
        if len(state.queue) >= MAX_QUEUE_LENGTH:
            queue_limit_reached = True
        else:
            cancel_idle_disconnect(state)
            state.queue.append(song)
            should_start = not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused())

    if queue_limit_reached:
        await ctx.send(f"⚠️ 目前佇列已達上限（{MAX_QUEUE_LENGTH} 首），請稍後再加入。")
        return

    if not should_start:
        await ctx.send(f"✅ 已加入佇列: **{song.title}** ({format_duration(song.duration)})")
        return

    await play_next(ctx)


@bot.hybrid_command(name="skip", help="跳過目前歌曲")
@app_commands.guild_only()
async def skip(ctx):
    if not await ensure_same_voice_channel(ctx):
        return

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("目前沒有正在播放的歌曲。")
        return
    ctx.voice_client.stop()
    await ctx.send("⏭️ 已跳過目前歌曲。")


@bot.hybrid_command(name="pause", help="暫停播放")
@app_commands.guild_only()
async def pause(ctx):
    if not await ensure_same_voice_channel(ctx):
        return

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("目前沒有正在播放的歌曲。")
        return
    ctx.voice_client.pause()
    await ctx.send("⏸️ 已暫停。")


@bot.hybrid_command(name="resume", help="繼續播放")
@app_commands.guild_only()
async def resume(ctx):
    if not await ensure_same_voice_channel(ctx):
        return

    if not ctx.voice_client or not ctx.voice_client.is_paused():
        await ctx.send("目前沒有暫停中的歌曲。")
        return
    ctx.voice_client.resume()
    await ctx.send("▶️ 已繼續播放。")


@bot.hybrid_command(name="stop", help="停止播放並清空佇列")
@app_commands.guild_only()
async def stop(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    if not await ensure_same_voice_channel(ctx):
        return

    state = get_state(ctx.guild.id)
    async with state.lock:
        state.queue.clear()
        state.now_playing = None

    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
    else:
        schedule_idle_disconnect(ctx.guild.id)

    await ctx.send("⏹️ 已停止播放並清空佇列。")


@bot.hybrid_command(name="queue", help="查看目前佇列")
@app_commands.guild_only()
async def queue_list(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    state = get_state(ctx.guild.id)
    queue_items = list(state.queue)

    if not state.now_playing and not queue_items:
        await ctx.send("目前沒有歌曲。")
        return

    lines = []
    if state.now_playing:
        lines.append(
            f"正在播放: **{state.now_playing.title}** ({format_duration(state.now_playing.duration)})"
        )
    if queue_items:
        lines.append("待播清單:")
        for idx, song in enumerate(queue_items[:10], start=1):
            lines.append(f"{idx}. {song.title} ({format_duration(song.duration)})")
        if len(queue_items) > 10:
            lines.append(f"...還有 {len(queue_items) - 10} 首")

    await ctx.send("\n".join(lines))


@bot.hybrid_command(name="now", help="顯示目前播放")
@app_commands.guild_only()
async def now(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    state = get_state(ctx.guild.id)
    if not state.now_playing:
        await ctx.send("目前沒有正在播放的歌曲。")
        return

    song = state.now_playing
    await ctx.send(f"🎧 現在播放: **{song.title}**\n連結: {song.webpage_url}")


@bot.hybrid_command(name="roll", help="隨機抽出目前頻道的一位使用者")
@app_commands.guild_only()
async def roll(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    channel_members = getattr(ctx.channel, "members", None)
    candidates = [member for member in channel_members or [] if not member.bot]

    if not candidates and ctx.author.voice and ctx.author.voice.channel:
        candidates = [member for member in ctx.author.voice.channel.members if not member.bot]

    if not candidates:
        await ctx.send("找不到可抽籤的使用者，請確認頻道內有可用成員。")
        return

    winner = random.choice(candidates)
    await ctx.send(f"🎲 抽籤結果：{winner.mention}")


@bot.hybrid_command(name="leave", help="讓機器人離開語音頻道")
@app_commands.guild_only()
async def leave(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    if not await ensure_same_voice_channel(ctx):
        return

    if ctx.voice_client:
        state = get_state(ctx.guild.id)
        async with state.lock:
            state.queue.clear()
            state.now_playing = None
            cancel_idle_disconnect(state)

        await ctx.voice_client.disconnect()
        cleanup_guild_state_if_idle(ctx.guild.id)
        await ctx.send("👋 已經離開語音頻道。")
    else:
        await ctx.send("我目前不在任何語音頻道裡面喔！")


@bot.event
async def on_command_error(ctx, error):
    await cleanup_user_command_message(ctx, from_error=True)

    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("參數不足，請檢查指令格式。")
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send("參數格式錯誤，請確認輸入內容。")
        return

    if isinstance(error, commands.CommandInvokeError) and error.original:
        await ctx.send(f"⚠️ 指令執行失敗：{classify_error(error.original)}")
        print(f"[command] invoke error: {error.original}")
        return

    await ctx.send(f"⚠️ 指令失敗：{classify_error(error)}")
    print(f"[command] unhandled error: {error}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError) and error.original:
        message = f"⚠️ 指令執行失敗：{classify_error(error.original)}"
        print(f"[app command] invoke error: {error.original}")
    elif isinstance(error, app_commands.MissingPermissions):
        message = "你沒有權限執行此斜線指令。"
    elif isinstance(error, app_commands.CheckFailure):
        message = "此斜線指令目前無法在這裡使用。"
    else:
        message = f"⚠️ 指令失敗：{classify_error(error)}"
        print(f"[app command] unhandled error: {error}")

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# 4. 啟動機器人 (請設定環境變數 DISCORD_BOT_TOKEN)
token = os.getenv("DISCORD_BOT_TOKEN")
if not token:
    raise RuntimeError("找不到 DISCORD_BOT_TOKEN，請先設定環境變數。")

bot.run(token)