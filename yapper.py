import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
import yt_dlp
from discord.ext import commands

# 1. 設定機器人的 Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 2. 設定 yt-dlp 與 FFmpeg 的參數
ytdl_format_options = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # 綁定 ipv4 避免某些 ipv6 造成的問題
}

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",  # 告訴 FFmpeg 不要處理影像，只要音訊
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)


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
    volume: float = 0.5
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


music_states: dict[int, GuildMusicState] = {}


def get_state(guild_id: int) -> GuildMusicState:
    state = music_states.get(guild_id)
    if state is None:
        state = GuildMusicState()
        music_states[guild_id] = state
    return state


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
            return

        next_song = state.queue.popleft()
        state.now_playing = next_song

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(next_song.stream_url, **ffmpeg_options),
            volume=state.volume,
        )

    def after_playback(error: Optional[Exception]) -> None:
        if error:
            print(f"播放發生錯誤: {error}")
        loop.call_soon_threadsafe(lambda: asyncio.create_task(play_next(ctx)))

    voice_client.play(source, after=after_playback)
    await ctx.send(f"🎵 現在正在播放: **{next_song.title}** ({format_duration(next_song.duration)})")


async def ensure_voice(ctx: commands.Context) -> bool:
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("你必須先加入一個語音頻道！")
        return False

    channel = ctx.author.voice.channel
    if ctx.voice_client is None:
        await channel.connect()
    elif ctx.voice_client.channel != channel:
        await ctx.voice_client.move_to(channel)
    return True


# 3. 建立指令
@bot.event
async def on_ready():
    print(f"成功登入！機器人名稱：{bot.user}")


@bot.command(name="join", help="讓機器人加入你的語音頻道")
async def join(ctx):
    if not await ensure_voice(ctx):
        return
    await ctx.send(f"已加入語音頻道：{ctx.voice_client.channel}")


@bot.command(name="play", help="播放 YouTube 音樂 (輸入: !play <網址或關鍵字>)")
async def play(ctx, *, query: str):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    if not await ensure_voice(ctx):
        return

    state = get_state(ctx.guild.id)
    state.text_channel_id = ctx.channel.id

    async with ctx.typing():
        try:
            song = await extract_song(query)
        except Exception as error:
            await ctx.send(f"播放時發生錯誤，可能是網址無效或被 YouTube 阻擋。({error})")
            return

    async with state.lock:
        state.queue.append(song)
        should_start = not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused())

    if not should_start:
        await ctx.send(f"✅ 已加入佇列: **{song.title}** ({format_duration(song.duration)})")
        return

    await play_next(ctx)


@bot.command(name="skip", help="跳過目前歌曲")
async def skip(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("目前沒有正在播放的歌曲。")
        return
    ctx.voice_client.stop()
    await ctx.send("⏭️ 已跳過目前歌曲。")


@bot.command(name="pause", help="暫停播放")
async def pause(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("目前沒有正在播放的歌曲。")
        return
    ctx.voice_client.pause()
    await ctx.send("⏸️ 已暫停。")


@bot.command(name="resume", help="繼續播放")
async def resume(ctx):
    if not ctx.voice_client or not ctx.voice_client.is_paused():
        await ctx.send("目前沒有暫停中的歌曲。")
        return
    ctx.voice_client.resume()
    await ctx.send("▶️ 已繼續播放。")


@bot.command(name="stop", help="停止播放並清空佇列")
async def stop(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    state = get_state(ctx.guild.id)
    async with state.lock:
        state.queue.clear()
        state.now_playing = None

    if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()

    await ctx.send("⏹️ 已停止播放並清空佇列。")


@bot.command(name="queue", help="查看目前佇列")
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


@bot.command(name="now", help="顯示目前播放")
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


@bot.command(name="leave", help="讓機器人離開語音頻道")
async def leave(ctx):
    if ctx.guild is None:
        await ctx.send("此指令只能在伺服器中使用。")
        return

    if ctx.voice_client:
        state = get_state(ctx.guild.id)
        async with state.lock:
            state.queue.clear()
            state.now_playing = None

        await ctx.voice_client.disconnect()
        await ctx.send("👋 已經離開語音頻道。")
    else:
        await ctx.send("我目前不在任何語音頻道裡面喔！")


# 4. 啟動機器人 (請設定環境變數 DISCORD_BOT_TOKEN)
token = os.getenv("DISCORD_BOT_TOKEN")
if not token:
    raise RuntimeError("找不到 DISCORD_BOT_TOKEN，請先設定環境變數。")

bot.run(token)