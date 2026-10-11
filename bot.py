"""Discord channel summariser.

Runs on HeilandBay; the LLM runs on hyperBox (ollama). Chat text never leaves
the LAN - the only outside traffic is to Discord itself.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import signal
import time
from collections import Counter
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

TOKEN = os.environ["DISCORD_TOKEN"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.0.208:11434").rstrip("/")
MODEL_FAST = os.environ.get("MODEL_FAST", "qwen3.8:27b")
MODEL_BIG = os.environ.get("MODEL_BIG", "qwen3.5:122b-a10b")
GUILD_ID = os.environ.get("GUILD_ID")          # set = instant command sync to that server
USAGE_LOG = os.environ.get("USAGE_LOG", "/data/usage.jsonl")   # who/when/outcome, never content
TZ = ZoneInfo(os.environ.get("TZ", "America/Chicago"))

MAX_COUNT = 5000        # cap for count=; time windows fetch everything in range
MAX_FETCH = 10_000      # hard stop on history fetch for any request
MAX_CHARS = 70_000      # per model call; chat runs ~2.5 chars/token, so ~28k of NUM_CTX
NUM_CTX = 32768
LLM_TIMEOUT = 600       # 122B on a long transcript can take minutes
DEADLINE = 14 * 60      # Discord followup tokens die at 15 min

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
        log.info("usage %s", json.dumps(rec, ensure_ascii=False))
        try:
            with open(USAGE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("couldn't write %s", USAGE_LOG)
        active -= 1


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


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    log.info("logged in as %s, ollama at %s", client.user, OLLAMA_URL)


async def drain_and_close():
    """SIGTERM (docker stop / redeploy): refuse new work, let running summaries
    finish, then disconnect. compose's stop_grace_period must cover this."""
    global shutting_down
    shutting_down = True
    log.info("SIGTERM - waiting for %d running summar%s", active, "y" if active == 1 else "ies")
    while active:
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
