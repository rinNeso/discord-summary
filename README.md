# discord-summary

A Discord `/summary` bot (**Recap-a-Bara**) for a friend-group server. It runs on
HeilandBay (TrueNAS) and gets its summaries from ollama on hyperBox, so **chat
text stays on the LAN**. The only outside traffic is to Discord.

```
Discord ──▶ bot (HeilandBay, /mnt/apps/discord-summary/stack)
               └──▶ ollama on hyperBox 192.168.0.208:11434 (qwen3.8:27b)
```

## Usage

| | |
|---|---|
| `/summary` | last 3 hours |
| `/summary since:9am` | since a time today (`14:30`, `2:15pm`) |
| `/summary hours:12` / `days:3` | rolling window, up to 7 days |
| `/summary count:200` | last N messages (max 5000) |
| `big:true` | use `qwen3.5:122b-a10b` |
| `public:true` | post to the channel instead of only to you |

Summaries are a **topic outline**: a bold topic line naming who was in it, then
short bullets, with sub-bullets for details. The bot normalises whatever the
model emits into that shape. A window inside one calendar day gets a single summary. Anything longer gets
**one section per day**, posted as each finishes. Private multi-day summaries
go to your **DMs** so they don't clog the channel (and DMs aren't bound by
Discord's 15-minute reply limit). If your DMs from server members are off, the
summary falls back to the channel, visible only to you. The header (message count,
span, who was talking) and the `processed in` footer come from the bot, not the
model.

## Deploy

On HeilandBay, the files live at `/mnt/apps/discord-summary/stack/`:

```
sudo docker compose up -d --build
```

Token and optional `GUILD_ID` go in `/mnt/apps/discord-summary/secrets/.env`
(root, 600) via `set-token.sh`, which reads them without echoing:

```
ssh -t truenas_admin@192.168.0.69 sudo /mnt/apps/discord-summary/set-token.sh
```

With `GUILD_ID` unset, the command registers globally, so it works on every
server the bot is invited to. Invite scopes are `bot applications.commands` with
permissions `68608` (view channels, send messages, read history). The bot needs
**Message Content Intent** on.

hyperBox side, tracked in `hyperbox-hw`: `OLLAMA_HOST=0.0.0.0:11434` in the
ollama override, plus `ufw allow proto tcp from 192.168.0.69 to any port 11434`.
Ollama has no auth, so that rule is the only gate. `check.sh` asserts it.

## Usage stats

```
sudo docker exec discord-summary python stats.py
```

Reads `/mnt/apps/discord-summary/data/usage.jsonl`: who, when, which server
and channel, the options, message count, duration and outcome
(`ok` / `empty` / `hyperbox_offline` / `timeout` / `deadline` / …).
**Message content and summaries are never logged.**

## Why it's built this way (benchmark, 2026-10-11)

One real week of #general (1,705 messages, 41.7k tokens) was run through both
models, as a single prompt and as day-by-day summaries combined at the end.
Each output was scored by hand against the full transcript on 20 key topics,
plus a list of easy-to-get-wrong facts.

| Setup | Topics (/20) | Errors | Time |
|---|---|---|---|
| 27B, whole week | ~13.5 | ~5 | 5.4 min |
| 122B, whole week | ~13 | ~6 | 4.3 min |
| 27B, per-day then combined | ~9 | ~7 | 5.75 min |
| 122B, per-day then combined | ~11 | ~7 | 5.0 min |

- **The 122B was not more accurate.** Both models mostly get *who said what*
  wrong. The 122B hedged better in places but invented a phrase.
- **The 122B is faster on long inputs.** It's MoE with ~10B active parameters
  versus 27B dense, on bandwidth-bound Strix Halo. The cost is 81 GB of RAM and
  ~53s cold load.
- **Models can't count.** Their guesses ranged from "~450" to "864 over 14
  days". So the bot writes the header.
- **Per-day summaries were the best output of all, until they were combined.**
  The combine step is what dropped topics, so the bot posts the days
  uncombined.
- At ~2.5 chars/token, the old 90k-char / 32k-ctx limits silently cut a week.
  Now each model call is capped at 70k chars, and long windows are split by day.

One week and one run each: treat small differences as noise. `bench/` holds
the harness: `extract.py` turns a DiscordChatExporter HTML export into
transcript lines, and `bench.py` runs the comparison. The chat data and model
outputs are deliberately **not** in this repo.
