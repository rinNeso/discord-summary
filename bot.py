"""Discord channel summariser, Instagram link reposter, and voice-chat TTS.

Runs on HeilandBay; the LLM runs on hyperBox (ollama), and TTS runs here (see
tts.py). Chat text never leaves the LAN. Outside traffic is Discord itself, plus
Instagram for any post someone links (see instagram.py) - only the link is
fetched, nothing from the chat is sent.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import signal
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import aiohttp
import discord
import random

from discord import app_commands
from discord.ext import tasks

import instagram
import tts

TOKEN = os.environ["DISCORD_TOKEN"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.0.208:11434").rstrip("/")
MODEL_FAST = os.environ.get("MODEL_FAST", "qwen3.8:27b")
MODEL_BIG = os.environ.get("MODEL_BIG", "qwen3.5:122b-a10b")
GUILD_ID = os.environ.get("GUILD_ID")          # set = instant command sync to that server
USAGE_LOG = os.environ.get("USAGE_LOG", "/data/usage.jsonl")   # who/when/outcome, never content
TZ = ZoneInfo(os.environ.get("TZ", "America/Chicago"))
INSTAGRAM = os.environ.get("INSTAGRAM", "1") == "1"   # repost Instagram links as files
# /tts is locked to one voice channel: the bot joins only it and reads only its
# built-in text chat (same ID). The commands are registered in that server alone,
# so nobody elsewhere even sees them. Either unset = /tts off.
TTS_GUILD = int(os.environ.get("TTS_GUILD") or 0)
TTS_CHANNEL = int(os.environ.get("TTS_CHANNEL") or 0)
# Only these user IDs (comma-separated) may use it; empty = nobody. Added after
# an open free-for-all was abused within minutes.
TTS_USERS = {int(u) for u in os.environ.get("TTS_USERS", "").split(",") if u.strip()}

MAX_COUNT = 5000        # cap for count=; time windows fetch everything in range
MAX_FETCH = 10_000      # hard stop on history fetch for any request
MAX_CHARS = 70_000      # per model call; chat runs ~2.5 chars/token, so ~28k of NUM_CTX
NUM_CTX = 32768
LLM_TIMEOUT = 600       # 122B on a long transcript can take minutes
DEADLINE = 14 * 60      # Discord followup tokens die at 15 min
IG_TIMEOUT = 180        # per Instagram link: download plus any re-encode
IG_MAX_LINKS = 3        # per message, so a pasted list can't flood the channel
TTS_QUEUE = 8           # messages waiting to be spoken, per server

PROMPT = """You summarise a Discord group chat for someone who missed it.

Write it as a topic outline, in exactly this shape:

**Topic name** - who was in it
- one point per bullet, under ~20 words
  - optional sub-point for a detail or reply
**Next topic** - who was in it
- ...

Rules:
- Topics in the order they came up; merge small exchanges into a "Misc" topic at the end.
- Bullets only - no paragraphs, no headings other than the bold topic lines.
- Say who said what, using the names as written.
- Keep jokes and banter as what they were; don't sanitise, moralise or editorialise.
- Lines that are only a GIF, image or emoji carry no content - don't summarise them.
- Don't count messages or list participants - that header is added for you.
- Stay under {limit} characters. No preamble, no closing offer.

Transcript ({tz}):
{transcript}"""

log = logging.getLogger("summary")
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
llm_lock = asyncio.Lock()
active = 0              # summaries in progress - SIGTERM waits for these
ig_jobs = 0             # Instagram reposts in progress - SIGTERM waits for these too
ig_sem = asyncio.Semaphore(2)
warned_perms: set[int] = set()  # channels already told about a missing permission

# Rotating custom statuses. Edit freely - one is picked every STATUS_EVERY minutes.
STATUS_EVERY = 15
STATUSES = [
    "vibing in a hot spring 🧖",
    "chilling with a yuzu on my head 🍊",
    "friends with literally everyone",
    "too chill to have opinions",
    "reading the backlog so you don't have to",
    "tl;dr-ing your drama",
    "scrolling up... still scrolling up...",
    "skimming 1,705 messages, judging none",
    "counting messages (the AI can't)",
    "fact-checking nobody, verify me 🫡",
    "running on a local LLM, not the cloud ☁️🚫",
    "powered by hyperBox and snacks",
    "waiting for scarlett to get out of bed",
    "brewing pumpkin spice for the group",
    "/summary me, cap'n",
    "capybara-certified recaps",
    "chewing on a nice reed 🌾",
    "sitting peacefully while chaos unfolds",
    "accepting head scritches",
    "in a group cuddle with some ducks 🦆",
]
BUSY_STATUSES = [
    "summarising... don't rush a capybara",
    "reading so you don't have to 📖",
    "chewing through the backlog 🌾",
]
shutting_down = False


def parse_since(text: str) -> dt.datetime:
    """'9am', '9:30', '14:00', '2:15pm' -> today at that local time."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", text.lower())
    if not m:
        raise ValueError(f"can't read {text!r} as a time - try 9am or 14:30")
    hour, minute, ampm = int(m[1]), int(m[2] or 0), m[3]
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"{text!r} isn't a valid time")
    now = dt.datetime.now(TZ)
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when <= now else when - dt.timedelta(days=1)


def describe(msg: discord.Message) -> str:
    name = msg.author.display_name
    extra = ""
    ref = msg.reference.resolved if msg.reference else None
    if isinstance(ref, discord.Message):
        extra += f" [reply to {ref.author.display_name}]"
    if msg.attachments:
        extra += f" [{len(msg.attachments)} attachment]"
    if msg.stickers:
        extra += " [sticker]"
    if any(e.type in ("gifv", "image") for e in msg.embeds):
        extra += " [gif]"
    text = msg.clean_content.strip().replace("\n", " / ")
    return f"{msg.created_at.astimezone(TZ):%H:%M} {name}{extra}: {text}"


async def ollama_up(session: aiohttp.ClientSession) -> bool:
    try:
        async with session.get(f"{OLLAMA_URL}/api/version",
                               timeout=aiohttp.ClientTimeout(total=4)) as r:
            return r.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def summarise(session: aiohttp.ClientSession, model: str, transcript: str,
                    limit: int = 1500) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT.format(tz=TZ.key, limit=limit,
                                               transcript=transcript)}],
        "stream": False,
        "think": False,
        "options": {"num_ctx": NUM_CTX},
    }
    timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT)
    async with session.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=timeout) as r:
        if r.status == 400 and "think" in (await r.text()).lower():
            body.pop("think")       # model has no thinking toggle
            async with session.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=timeout) as r2:
                r2.raise_for_status()
                data = await r2.json()
        else:
            r.raise_for_status()
            data = await r.json()
    out = data["message"]["content"]
    return outline(re.sub(r"<think>.*?</think>", "", out, flags=re.S))


def outline(text: str) -> str:
    """Coerce model output into the topic outline the prompt asks for.

    Models drift: '##' headings (the bot owns headings), '*   ' bullets, ragged
    indents. Discord renders '- ' and '  - ' cleanly, so normalise to those.
    """
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        if m := re.match(r"\s*#{1,6}\s+(.*)", line):
            line = f"**{m[1].strip('* ')}**"
        elif m := re.match(r"(\s*)[*\u2022+-]\s+(.*)", line):
            line = ("  - " if len(m[1].expandtabs(4)) >= 2 else "- ") + m[2]
        if line.startswith("**") and out:
            out.append("")          # blank line before each topic
        out.append(line.rstrip())
    return "\n".join(out)


def fit(lines: list[str]) -> tuple[list[str], int]:
    """Drop oldest lines until the transcript fits one model call."""
    dropped = 0
    while sum(len(l) + 1 for l in lines) > MAX_CHARS:
        lines.pop(0)
        dropped += 1
    return lines, dropped


def header(msgs: list[discord.Message]) -> str:
    """Counted by the bot, not the model - models can't count (benchmarked)."""
    first, last = (m.created_at.astimezone(TZ) for m in (msgs[0], msgs[-1]))
    span = (f"{first:%a %b %-d %H:%M}–{last:%H:%M}" if first.date() == last.date()
            else f"{first:%a %b %-d %H:%M} – {last:%a %b %-d %H:%M}")
    people = Counter(m.author.display_name for m in msgs).most_common()
    names = ", ".join(n for n, _ in people[:8])
    if len(people) > 8:
        names += f" +{len(people) - 8} more"
    return f"**{len(msgs)} messages** · {span} · {names}"


def chunks(text: str, size: int = 1900):
    while text:
        if len(text) <= size:
            cut = len(text)
        else:   # prefer a topic boundary, so a heading never ends a message
            cut = text.rfind("\n\n", 0, size)
            cut = cut if cut > size // 3 else text.rfind("\n", 0, size)
            cut = cut if cut > 0 else size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


@tree.command(name="summary", description="Summarise recent messages in this channel")
@app_commands.describe(
    since="Start time today, e.g. 9am or 14:30 (default: last 3 hours)",
    hours="How many hours back to go",
    days="How many days back to go (1-7); longer windows are summarised day by day",
    count=f"Summarise the last N messages instead (max {MAX_COUNT})",
    big="Use the large 122B model",
    public="Post the summary to the channel instead of only to you",
)
async def summary(interaction: discord.Interaction, since: str | None = None,
                  hours: app_commands.Range[float, 0.1, 168.0] = 3.0,
                  days: app_commands.Range[int, 1, 7] | None = None,
                  count: app_commands.Range[int, 1, MAX_COUNT] | None = None,
                  big: bool = False, public: bool = False):
    global active
    if shutting_down:
        await interaction.response.send_message(
            "I'm restarting for an update - try again in a minute.", ephemeral=True)
        return
    active += 1
    await set_status(random.choice(BUSY_STATUSES))
    rec = dict(ts=dt.datetime.now(TZ).isoformat(timespec="seconds"),
               user=interaction.user.name, guild=getattr(interaction.guild, "name", None),
               channel=getattr(interaction.channel, "name", None),
               since=since, hours=None if since or count or days else hours, days=days,
               count=count, big=big, public=public, outcome="crash")
    t0 = time.monotonic()
    try:
        rec["outcome"] = await run_summary(interaction, rec, since, hours, days, count,
                                           big, public)
    except Exception:
        log.exception("summary crashed")
        raise
    finally:
        rec["secs"] = round(time.monotonic() - t0, 1)
        record(rec)
        active -= 1
        if not active:
            await set_status(random.choice(STATUSES))


def record(rec: dict):
    """Append to the usage log: who, when, options, outcome - never content."""
    log.info("usage %s", json.dumps(rec, ensure_ascii=False))
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("couldn't write %s", USAGE_LOG)


def by_day(msgs: list[discord.Message]) -> list[tuple[dt.date, list[discord.Message]]]:
    groups: dict[dt.date, list[discord.Message]] = {}
    for m in msgs:
        groups.setdefault(m.created_at.astimezone(TZ).date(), []).append(m)
    return list(groups.items())


async def run_summary(interaction, rec, since, hours, days, count, big, public) -> str:
    """Returns the outcome label recorded in the usage log.

    One calendar day -> one summary. Several days -> one summary per day, posted
    as sections. Benchmarked on a real week: per-day summaries were the most
    accurate output from either model, and squeezing them into one combined
    summary is what lost topics.
    """
    t0 = time.monotonic()
    await interaction.response.defer(ephemeral=not public, thinking=True)
    send = interaction.followup.send
    now = dt.datetime.now(TZ)
    try:
        if count:
            after, limit = None, count
        elif since:
            after, limit = parse_since(since), MAX_FETCH
        elif days:
            after, limit = now - dt.timedelta(days=days), MAX_FETCH
        else:
            after, limit = now - dt.timedelta(hours=hours), MAX_FETCH
    except ValueError as e:
        await send(str(e), ephemeral=True)
        return "bad_time"

    try:
        msgs = [m async for m in interaction.channel.history(limit=limit, after=after,
                                                             oldest_first=False)]
    except discord.Forbidden:
        await send("I can't read this channel's history - check my permissions.",
                   ephemeral=True)
        return "no_permission"
    msgs = [m for m in reversed(msgs) if not m.author.bot and m.type in
            (discord.MessageType.default, discord.MessageType.reply)]
    if not msgs:
        await send("Nothing to summarise in that window.", ephemeral=True)
        return "empty"

    groups = by_day(msgs)
    model = MODEL_BIG if big else MODEL_FAST
    rec.update(messages=len(msgs), days_spanned=len(groups), model=model)
    head = header(msgs)
    dropped = 0
    async with aiohttp.ClientSession() as session:
        if not await ollama_up(session):
            await send("hyperBox is offline or asleep, so I can't summarise "
                       "right now. Try again later.", ephemeral=True)
            return "hyperbox_offline"

        # Private multi-day summaries go to DMs: several sections clog the channel
        # even when only you can see them. DMs also escape the 15-minute followup
        # limit. Falls back to the channel if the user has server DMs turned off.
        dm = None
        rec["delivery"] = "channel" if public else "ephemeral"
        if len(groups) > 1 and not public:
            where = getattr(interaction.channel, "jump_url", "")
            try:
                dm = await interaction.user.create_dm()
                await dm.send(f"{head}\n-# {len(groups)} days from {where} - one section "
                              "per day, posted as each finishes")
                rec["delivery"] = "dm"
                await send(f"Sending this one to your DMs - {len(groups)} days, one "
                           "section each.", ephemeral=True)
            except discord.HTTPException:   # Forbidden = DMs from server members off
                dm = None
                rec["delivery"] = "dm_blocked"
                await send("-# Couldn't DM you (your privacy settings block it), so "
                           "it's going here instead.", ephemeral=True)

        async def post(text, error=False):
            if dm:
                await dm.send(text)
            else:
                await send(text, ephemeral=error or not public)
        if llm_lock.locked():
            await post("Another summary is running - you're next.", error=True)
        async with llm_lock:
            try:
                if len(groups) == 1:
                    lines, dropped = fit([describe(m) for m in msgs])
                    body = await summarise(session, model, "\n".join(lines))
                    sections = [f"{head}\n\n{body}"]
                else:
                    if not dm:
                        await post(f"{head}\n-# {len(groups)} days - one section per day, "
                                   "posted as each finishes")
                    sections = []
                    for i, (day, day_msgs) in enumerate(groups):
                        if not dm and time.monotonic() - t0 > DEADLINE - 90:
                            await post(f"-# Stopped before {day:%a %b %-d} - Discord only "
                                       "allows a reply 15 minutes to finish. Ask for a "
                                       "shorter window to get the rest.")
                            rec["days_done"] = i
                            return "deadline"
                        lines, d = fit([describe(m) for m in day_msgs])
                        dropped += d
                        body = await summarise(session, model, "\n".join(lines), limit=900)
                        text = f"### {day:%a %b %-d} · {len(day_msgs)} messages\n{body}"
                        for p in chunks(text):
                            await post(p)
                    rec["days_done"] = len(groups)
            except asyncio.TimeoutError:
                await post("hyperBox took too long - try a shorter window, "
                           "or without `big`.", error=True)
                return "timeout"
            except aiohttp.ClientError as e:
                log.exception("ollama call failed")
                await post(f"hyperBox returned an error: {e}", error=True)
                return "llm_error"

        rec["dropped"] = dropped
        secs = round(time.monotonic() - t0)
        took = f"{secs}s" if secs < 60 else f"{secs // 60}m {secs % 60}s"
        note = f"-# {model} · processed in {took}"
        if dropped:
            note += f" · oldest {dropped} messages of a very long day skipped"
        # Benchmarked: both models misattribute who-said-what in most summaries.
        note += ("\n-# ⚠️ AI-generated summary - it can get details and who said "
                 "what wrong. Check the chat before relying on anything here.")
        if sections:
            sections[-1] += "\n" + note
        else:
            sections = [note]
        for text in sections:
            for p in chunks(text):
                await post(p)
    return "ok"


@dataclass
class Speaker:
    channel_id: int         # the text channel whose messages get read out
    voice: str
    since: float = field(default_factory=time.monotonic)
    spoken: int = 0


@dataclass
class VoiceSession:
    """One per server: a bot can only be in one voice channel per server."""
    vc: discord.VoiceClient
    speakers: dict[int, Speaker] = field(default_factory=dict)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(TTS_QUEUE))
    task: asyncio.Task | None = None


voice_sessions: dict[int, VoiceSession] = {}

tts_group = app_commands.Group(
    name="tts", description="Read your messages aloud in voice chat, for when you have no mic",
    guild_only=True)
if TTS_GUILD and TTS_CHANNEL:
    tree.add_command(tts_group, guild=discord.Object(id=TTS_GUILD))


@tts_group.command(name="start",
                   description="Read what you type in this voice channel's chat aloud")
@app_commands.describe(voice="Which voice reads your messages")
@app_commands.choices(voice=[app_commands.Choice(name=n, value=v) for n, v in
                             [("Lessac (default)", "lessac"), ("Amy", "amy"), ("Ryan", "ryan")]])
async def tts_start(interaction: discord.Interaction, voice: str = tts.DEFAULT_VOICE):
    rec = dict(ts=dt.datetime.now(TZ).isoformat(timespec="seconds"), kind="tts",
               user=interaction.user.name, guild=interaction.guild.name, voice=voice)
    reply = interaction.response.send_message
    if shutting_down:
        await reply("I'm restarting for an update - try again in a minute.", ephemeral=True)
        return
    vchan = interaction.guild.get_channel(TTS_CHANNEL)
    if interaction.user.id not in TTS_USERS:
        record(dict(rec, outcome="not_allowed"))
        await reply("TTS is only switched on for specific people - ask Rin if you need it.",
                    ephemeral=True)
        return
    if interaction.guild.id != TTS_GUILD or not vchan:
        await reply("TTS isn't available here.", ephemeral=True)
        return
    if interaction.channel.id != TTS_CHANNEL:
        await reply(f"TTS only works in {vchan.mention}'s own chat - open the voice "
                    "channel's chat and run it there.", ephemeral=True)
        return
    if not interaction.user.voice or interaction.user.voice.channel != vchan:
        await reply(f"Join {vchan.mention} first, then run this again.", ephemeral=True)
        return
    rec["channel"] = vchan.name
    perms = vchan.permissions_for(interaction.guild.me)
    if not (perms.connect and perms.speak):
        record(dict(rec, outcome="no_permission"))
        await reply(f"I need the **Connect** and **Speak** permissions in {vchan.mention}.",
                    ephemeral=True)
        return
    sess = voice_sessions.get(interaction.guild.id)
    if not sess:
        await interaction.response.defer(thinking=True)
        reply = interaction.followup.send
        try:
            vc = await vchan.connect(self_deaf=True, timeout=20)
        except (asyncio.TimeoutError, discord.ClientException) as e:
            log.warning("voice connect failed: %r", e)
            record(dict(rec, outcome="connect_failed"))
            await reply("Couldn't join the voice channel - try again in a moment.",
                        ephemeral=True)
            return
        sess = voice_sessions[interaction.guild.id] = VoiceSession(vc)
        sess.task = asyncio.create_task(speak_loop(sess))
    old = sess.speakers.get(interaction.user.id)
    sess.speakers[interaction.user.id] = Speaker(interaction.channel.id, voice)
    if old:     # re-running it just changes the voice
        sess.speakers[interaction.user.id].since = old.since
        sess.speakers[interaction.user.id].spoken = old.spoken
    await reply(f"🔊 Reading **{discord.utils.escape_markdown(interaction.user.display_name)}**'s "
                f"messages in this chat aloud.\n"
                "-# `/tts stop` to end it. Links, emoji-only messages and anything "
                "starting with `//` are skipped.",
                allowed_mentions=discord.AllowedMentions.none())


@tts_group.command(name="stop", description="Stop reading your messages aloud")
async def tts_stop(interaction: discord.Interaction):
    sess = voice_sessions.get(interaction.guild.id)
    if not sess or interaction.user.id not in sess.speakers:
        await interaction.response.send_message("I wasn't reading your messages.",
                                                ephemeral=True)
        return
    await interaction.response.send_message("🔇 Stopped reading your messages.", ephemeral=True)
    await drop_speaker(sess, interaction.user.id, "stopped")


async def drop_speaker(sess: VoiceSession, user_id: int, outcome: str):
    sp = sess.speakers.pop(user_id, None)
    if sp:
        member = sess.vc.guild.get_member(user_id)
        record(dict(ts=dt.datetime.now(TZ).isoformat(timespec="seconds"), kind="tts",
                    user=member.name if member else str(user_id), guild=sess.vc.guild.name,
                    channel=getattr(sess.vc.channel, "name", None), voice=sp.voice,
                    outcome=outcome, messages=sp.spoken,
                    secs=round(time.monotonic() - sp.since)))
    if not sess.speakers:
        await end_session(sess, outcome)


async def end_session(sess: VoiceSession, outcome: str):
    guild = sess.vc.guild
    if voice_sessions.get(guild.id) is not sess:
        return              # already ended
    del voice_sessions[guild.id]
    for uid in list(sess.speakers):
        await drop_speaker(sess, uid, outcome)    # logs each; session's already gone
    if sess.task and sess.task is not asyncio.current_task():
        sess.task.cancel()
    if sess.vc.is_connected():
        await sess.vc.disconnect()


async def speak_loop(sess: VoiceSession):
    loop = asyncio.get_running_loop()
    while True:
        user_id, text = await sess.queue.get()
        sp = sess.speakers.get(user_id)
        if not sp or not sess.vc.is_connected():
            continue
        member = sess.vc.guild.get_member(user_id)
        if member:          # always, so the VC knows who's typing
            text = f"{member.display_name} says: {text}"
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", prefix="tts-") as f:
                await asyncio.to_thread(tts.synth, text, sp.voice, f.name)
                done = asyncio.Event()
                sess.vc.play(discord.FFmpegPCMAudio(f.name, options="-loglevel error"),
                             after=lambda e: loop.call_soon_threadsafe(done.set))
                await done.wait()
            sp.spoken += 1
        except asyncio.CancelledError:
            raise
        except Exception:   # one bad message mustn't kill the session
            log.exception("tts playback failed")


async def tts_message(message: discord.Message):
    sess = voice_sessions.get(message.guild.id)
    sp = sess and sess.speakers.get(message.author.id)
    if (not sp or message.author.id not in TTS_USERS
            or message.channel.id != TTS_CHANNEL or message.channel.id != sp.channel_id
            or message.content.startswith("//")):
        return
    text = tts.speakable(message.clean_content)
    if not text:
        return
    try:
        sess.queue.put_nowait((message.author.id, text))
    except asyncio.QueueFull:   # typing faster than it can talk: drop, don't lag minutes behind
        log.info("tts queue full in %s, dropped a message", message.guild.name)


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState,
                                after: discord.VoiceState):
    sess = voice_sessions.get(member.guild.id)
    if not sess:
        return
    if member == member.guild.me:
        if after.channel is None:       # kicked, or the channel was deleted
            await end_session(sess, "disconnected")
        elif after.channel.id != TTS_CHANNEL:   # dragged elsewhere: never read there
            await end_session(sess, "moved")
        return
    here = sess.vc.channel
    if member.id in sess.speakers and after.channel != here:
        await drop_speaker(sess, member.id, "left_voice")
    elif voice_sessions.get(member.guild.id) is sess and not any(
            not m.bot for m in here.members):
        await end_session(sess, "channel_empty")


@client.event
async def on_message(message: discord.Message):
    """Read out /tts speakers' messages; repost Instagram links as attachments."""
    if shutting_down or message.author.bot or not message.guild:
        return
    await tts_message(message)
    if not INSTAGRAM:
        return
    links = instagram.find_links(message.content)
    if not links:
        return
    perms = message.channel.permissions_for(message.guild.me)
    if not (perms.send_messages and perms.attach_files):
        # Say so once per channel, rather than silently doing nothing forever.
        if perms.send_messages and message.channel.id not in warned_perms:
            warned_perms.add(message.channel.id)
            await message.channel.send("-# I'd repost that Instagram link for people "
                                       "without an account, but I need the **Attach "
                                       "Files** permission in this channel.")
        return
    for link in links[:IG_MAX_LINKS]:
        await repost(message, link)


async def ig_reply(message: discord.Message, content: str | None = None, files=None):
    # Captions are user text: an "@everyone" in one must not ping the server.
    await message.channel.send(
        content, files=files or None, mention_author=False,
        reference=message.to_reference(fail_if_not_exists=False),
        allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)


def ig_caption(post: instagram.Post, spoiler: bool) -> str:
    who = discord.utils.escape_markdown(f"@{post.author}" if post.author else "Instagram")
    cap = " ".join(post.caption.split())
    if len(cap) > 200:
        cap = cap[:199].rstrip() + "…"
    line = f"-# 📸 **{who}**"
    if cap:
        cap = discord.utils.escape_markdown(cap)
        line += f" · ||{cap}||" if spoiler else f" · {cap}"
    return line


IG_UNAVAILABLE = {
    "private": "-# That Instagram post is from a private account, so it can't be reposted.",
    "restricted": "-# That Instagram post is age-restricted, and the bot couldn't get past it.",
    "other": "-# Couldn't fetch that Instagram post - it may be deleted, or Instagram "
             "is refusing for now.",
}


async def repost(message: discord.Message, link: instagram.Link):
    global ig_jobs
    ig_jobs += 1
    rec = dict(ts=dt.datetime.now(TZ).isoformat(timespec="seconds"), kind="instagram",
               user=message.author.name, guild=message.guild.name,
               channel=getattr(message.channel, "name", None), outcome="crash")
    t0 = time.monotonic()
    limit = message.guild.filesize_limit     # 10 MiB unboosted, more with boosts
    try:
        async with ig_sem:
            with tempfile.TemporaryDirectory(prefix="ig-") as tmp:
                try:
                    async with message.channel.typing():
                        post = await asyncio.wait_for(
                            asyncio.to_thread(instagram.fetch, link, tmp, limit), IG_TIMEOUT)
                except instagram.Unavailable as e:
                    log.info("instagram unavailable: %s", e)
                    rec.update(outcome="unavailable", reason=e.reason)
                    await ig_reply(message, IG_UNAVAILABLE[e.reason])
                    return
                except asyncio.TimeoutError:
                    rec["outcome"] = "timeout"
                    await ig_reply(message, "-# That Instagram post took too long to fetch.")
                    return
                rec.update(items=len(post.media), logged_in=post.logged_in,
                           videos=sum(m.kind == "video" for m in post.media),
                           compressed=sum(m.compressed for m in post.media),
                           too_big=post.too_big,
                           mb=round(sum(os.path.getsize(m.path) for m in post.media) / 2**20, 1))
                text = ig_caption(post, link.spoiler)
                if post.too_big:
                    text += (f"\n-# {post.too_big} video{'s' if post.too_big > 1 else ''} "
                             f"too long to fit under this server's {limit >> 20} MB upload limit.")
                groups = instagram.batches(post.media, limit)
                for i, group in enumerate(groups or [[]]):
                    files = [discord.File(m.path, filename=os.path.basename(m.path),
                                          spoiler=link.spoiler) for m in group]
                    await ig_reply(message, text if i == 0 else None, files)
                rec["outcome"] = "ok" if post.media else "too_big"
    except discord.HTTPException as e:
        log.warning("instagram repost: Discord refused the upload: %s", e)
        rec["outcome"] = "send_failed"
    except Exception:
        log.exception("instagram repost crashed")
    finally:
        rec["secs"] = round(time.monotonic() - t0, 1)
        record(rec)
        ig_jobs -= 1


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    if TTS_GUILD:   # with TTS_CHANNEL unset this syncs nothing, removing /tts there
        await tree.sync(guild=discord.Object(id=TTS_GUILD))
    log.info("logged in as %s, ollama at %s", client.user, OLLAMA_URL)
    if not rotate_status.is_running():
        rotate_status.start()
    await asyncio.to_thread(tts.preload)     # ~2s per voice, so not on someone's first message


async def set_status(text: str):
    try:
        await client.change_presence(activity=discord.CustomActivity(name=text))
    except Exception:       # cosmetic - never let a status break a summary
        log.exception("couldn't set status")


@tasks.loop(minutes=STATUS_EVERY)
async def rotate_status():
    if not active:          # leave the busy status alone mid-summary
        await set_status(random.choice(STATUSES))


async def drain_and_close():
    """SIGTERM (docker stop / redeploy): refuse new work, let running summaries
    finish, then disconnect. compose's stop_grace_period must cover this."""
    global shutting_down
    shutting_down = True
    for sess in list(voice_sessions.values()):   # nothing to finish; just log and leave
        await end_session(sess, "shutdown")
    log.info("SIGTERM - waiting for %d running summar%s and %d repost%s", active,
             "y" if active == 1 else "ies", ig_jobs, "" if ig_jobs == 1 else "s")
    while active or ig_jobs:
        await asyncio.sleep(1)
    await client.close()


async def main():
    tasks = set()   # keep a reference so the drain task can't be garbage-collected
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, lambda: tasks.add(asyncio.create_task(drain_and_close())))
    async with client:
        await client.start(TOKEN)


if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO)
    asyncio.run(main())
