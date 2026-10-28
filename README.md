# discord-summary

A Discord `/summary` bot (**Recap-a-Bara**) for a friend-group server. It runs on
HeilandBay (TrueNAS) and gets its summaries from ollama on hyperBox, so **chat
text stays on the LAN**. Outside traffic is Discord, plus Instagram when someone
posts an Instagram link (see below). Only the link itself is fetched; nothing
from the chat is sent anywhere.

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
Every summary ends with a disclaimer that it's AI-generated and should be
checked against the chat. The benchmark below is why: misattribution is the
models' most common error.

## Instagram links

Anyone without an Instagram account just sees a login wall, so the bot reposts
Instagram links as attachments. A post, reel or IGTV link in any channel the bot
can see gets a reply with the photos or video attached, plus a one-line caption
(`@account · caption`).

- **Carousels** come through in full: up to 10 attachments per message, split
  across messages when the post is bigger than that or than the upload limit.
- **Videos** arrive as H.264 MP4, which every Discord client plays. Anything over
  the server's upload limit (10 MB unboosted, 50 MB at boost level 2, 100 MB at
  level 3) is re-encoded to fit. If it would be unwatchable at that size, the
  bot says it's too long instead.
- `<link>` is left alone, since that's how people say "no preview". Links in
  `||spoilers||` are reposted as spoilers, caption included.
- At most 3 links per message, 2 downloads at a time.
- Posts it can't fetch get a short reply saying why: private account,
  age-restricted, or anything else (deleted, stories, Instagram refusing).
- Captions are sent with mentions disabled, so an `@everyone` in a caption
  can't ping the server.

It needs the **Attach Files** permission. Without it, the bot says so once per
channel. Set `INSTAGRAM: "0"` in the compose file to turn the feature off.

Everything is fetched logged out with yt-dlp (`instagram.py` explains how photos
are pulled out of it). When Instagram changes something and reposts start
failing, the fix is almost always a newer yt-dlp. It's unpinned, so a fresh
build picks it up:

```
sudo docker compose build --no-cache && sudo docker compose up -d
```

### Logging in (optional)

Age-restricted posts and private accounts are only visible when logged in.
`ig-login.sh` signs an account in once and saves **only its session cookies** to
`/mnt/apps/discord-summary/instagram/cookies.txt` (owned by the bot, mode 600);
the password is never stored. The bot still tries every link **logged out first**
and uses the session only when that fails. When the session expires the log says
`still age-restricted while logged in`; run the script again. Delete
`cookies.txt` to go back to logged out only.

## TTS in voice chat

> **Allowlist only.** Open to everyone on 2026-10-21, it was abused within
> minutes. Now only the user IDs in `TTS_USERS` (`.env`, comma-separated)
> can start it or be read out. Others get a polite no
> (logged as `not_allowed`). Remove `TTS_CHANNEL` from the `.env` to switch it off entirely.

For someone without a mic: they type, the bot says it in the voice channel.
It's **locked to one voice channel** (`TTS_GUILD` / `TTS_CHANNEL`, set in the
root-only `.env` with the token). The `/tts` commands are registered in that
server only, the bot joins only that VC, and it reads only that VC's built-in
text chat. If someone drags it into another channel, it leaves.

| | |
|---|---|
| `/tts start` | run it in the VC's chat while you're in the VC; your messages there get read out |
| `voice:` | Lessac (default), Amy or Ryan |
| `/tts stop` | stop reading yours; the bot leaves when nobody's left using it |

- Every message starts with the sender's name ("Alex says: …").
  Several people can use it at once.
- Skipped: bare links, emoji-only messages, attachments, and anything starting
  with `//`. Links inside a sentence are read as "link", custom emoji by name,
  and spoilers as "spoiler". Messages are cut at 300 characters.
- At most 8 messages can be waiting. Past that, new ones are dropped, so it never
  ends up minutes behind the chat.
- It also ends when the user leaves the VC or the VC empties.

The voices are [Piper](https://github.com/OHF-Voice/piper1-gpl), and they run on
the NAS CPU (~0.15 s per sentence), so **the text goes nowhere but Discord**.
They're downloaded into the image at build time. Voice needs `discord.py[voice]`,
which pulls in `davey` for Discord's DAVE end-to-end voice encryption, required
for every voice connection. The bot needs **Connect** and **Speak** in the VC.

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
permissions `101376` (view channels, send messages, read history, attach files).
Servers that invited it before the Instagram feature have `68608`, which lacks
Attach Files. Grant it on the bot's role, or re-invite with the new number. The bot needs
**Message Content Intent** on, which also lets it see Instagram links.

hyperBox side, tracked in `hyperbox-hw`: `OLLAMA_HOST=0.0.0.0:11434` in the
ollama override, plus `ufw allow proto tcp from 192.168.0.69 to any port 11434`.
Ollama has no auth, so that rule is the only gate. `check.sh` asserts it.

Redeploying is safe mid-summary. On SIGTERM the bot refuses new commands
("restarting, try again in a minute"), waits for running summaries to finish,
then exits. `stop_grace_period: 16m` gives it room.

## Usage stats

```
sudo docker exec discord-summary python stats.py
```

Reads `/mnt/apps/discord-summary/data/usage.jsonl`: who, when, which server
and channel, the options, message count, duration and outcome
(`ok` / `empty` / `hyperbox_offline` / `timeout` / `deadline` / …).
`/tts` logs one row per person per session (`kind: tts`): voice, how many
messages were read and for how long, and how it ended (`stopped` /
`left_voice` / `channel_empty` / `moved` / `disconnected` / `shutdown`).
Instagram reposts are logged the same way, as `kind: instagram`, with file
counts, outcome (`ok` / `unavailable` / `too_big` / `timeout` / `send_failed`),
the reason for `unavailable`, and whether the login was needed.
**Message content, summaries, spoken text, links and captions are never logged.**

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
