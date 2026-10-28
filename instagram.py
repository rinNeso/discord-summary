"""Instagram links -> attachments the whole server can see.

Instagram puts most posts behind a login wall, so a link in chat is useless to
anyone without an account. The bot fetches the post logged out and re-posts the
media as files.

Everything comes from yt-dlp. It downloads videos directly. Photos make it fail
with "no video formats" unless told to ignore that, and then it still returns
the post's metadata, where each photo is the entry's `thumbnail` at original
resolution (checked 2026-10-18: `stp=dst-jpg_e35_tt6` with no size suffix; the
other `thumbnails` are cropped or resized). Carousels come back as a playlist
with one entry per slide, so mixed photo/video posts fall out of the same code.

Logged-out access breaks whenever Instagram changes something. The fix is
almost always a newer yt-dlp: rebuild the image (see README).

Age-restricted posts, and private accounts, need a login. If ig-login.sh has
saved a session, a post that fails logged out is retried with it. Logged out
always goes first, so the account is used as little as possible: Instagram bans
accounts that look like scrapers.
"""
import glob
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

import yt_dlp

log = logging.getLogger("instagram")

# Posts, reels and IGTV. Stories and profiles need a login, so they're not matched.
LINK = re.compile(r"https?://(?:www\.|m\.)?instagram\.com/(?:(?!share/)[A-Za-z0-9_.]+/)?"
                  r"(?:p|reels?|tv)/([A-Za-z0-9_-]+)[^\s<>|]*", re.I)
# The mobile share sheet sometimes hands out these instead; they redirect.
SHARE = re.compile(r"https?://(?:www\.)?instagram\.com/share/[A-Za-z0-9_/-]+", re.I)
SPOILER = re.compile(r"\|\|.+?\|\|", re.S)

COOKIES = "/instagram/cookies.txt"  # written by ig-login.sh; absent = logged out only
MAX_SOURCE = 300 * 2**20    # refuse anything absurd before it hits the disk
MIN_VIDEO_BPS = 200_000     # below this a re-encode isn't worth watching
AUDIO_BPS = 96_000


@dataclass
class Link:
    url: str
    spoiler: bool
    share: bool = False


@dataclass
class Media:
    path: str
    kind: str               # "video" or "image"
    compressed: bool = False


@dataclass
class Post:
    author: str = ""
    caption: str = ""
    media: list[Media] = field(default_factory=list)
    too_big: int = 0        # videos that couldn't be squeezed under the limit
    logged_in: bool = False # needed the account's session


class Unavailable(Exception):
    """Private, deleted, login-walled or rate-limited - nothing we can fix.

    `reason` is "private", "restricted" (age or sensitive-content gate) or
    "other", so the bot can tell people which.
    """
    def __init__(self, msg: str):
        super().__init__(msg)
        m = msg.lower()
        if "follow this account" in m or "private" in m:
            self.reason = "private"
        elif "available to everyone" in m or "certain audiences" in m:
            self.reason = "restricted"
        else:
            self.reason = "other"


def find_links(text: str) -> list[Link]:
    """Instagram links in a message, deduplicated, in order.

    `<link>` is how Discord users say "no preview", so those are left alone.
    Links inside ||spoilers|| are reposted as spoilers.
    """
    spoilers = [m.span() for m in SPOILER.finditer(text)]
    shares = [m.span() for m in SHARE.finditer(text)]
    out, seen = [], set()
    for pattern, share in ((LINK, False), (SHARE, True)):
        for m in pattern.finditer(text):
            start, end = m.span()
            if start and text[start - 1] == "<" and text[end:end + 1] == ">":
                continue
            if not share and any(a <= start < b for a, b in shares):
                continue    # /share/reel/X is a share token, not post X
            # Normalise to the canonical post URL: drops ?igsh= tracking and
            # makes /reel/X and /p/X the same post for dedup.
            url = m[0] if share else f"https://www.instagram.com/p/{m[1]}/"
            if url in seen:
                continue
            seen.add(url)
            out.append(Link(url, any(a <= start < b for a, b in spoilers), share))
    return out


def _sniff(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _duration(path: str) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def _shrink(src: str, limit: int) -> str | None:
    """Re-encode a video to fit `limit` bytes, or None if it can't be done well.

    Bitrate is budgeted from the duration with headroom for container overhead,
    then retried once a notch lower if x264 overshoots.
    """
    secs = _duration(src)
    if not secs:
        return None
    video_bps = limit * 8 * 0.92 / secs - AUDIO_BPS
    if video_bps < MIN_VIDEO_BPS:
        return None
    out = os.path.splitext(src)[0] + "-small.mp4"
    for factor in (1.0, 0.8):
        rate = int(video_bps * factor)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", src,
             "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(rate),
             "-maxrate", str(int(rate * 1.5)), "-bufsize", str(rate * 2),
             "-vf", "scale='min(720,iw)':-2", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", str(AUDIO_BPS), "-movflags", "+faststart", out],
            check=True, timeout=600)
        if os.path.getsize(out) <= limit:
            return out
    return None


class _Quiet:
    """yt-dlp prints errors to stderr even with quiet=True. Expected failures
    (login walls, deleted posts) are raised as Unavailable and logged once by
    the caller, so its own output only matters when debugging."""
    def debug(self, msg): log.debug(msg)
    info = warning = error = debug


def _resolve_share(ydl, url: str) -> str:
    """/share/... links redirect to the real post; follow them first."""
    try:
        final = ydl.urlopen(url).url
    except Exception as e:
        raise Unavailable(f"share link didn't resolve: {e}") from None
    m = LINK.match(final)
    if not m:
        raise Unavailable("share link didn't lead to a post")
    return f"https://www.instagram.com/p/{m[1]}/"


def fetch(link: Link, workdir: str, limit: int) -> Post:
    """Download every photo and video in a post into `workdir`. Blocking.

    Logged out first, then with the saved session if there is one. yt-dlp
    rewrites its cookie file on exit, so it gets a scratch copy: two downloads
    at once can't clobber each other, and a bad run can't damage the session.
    """
    try:
        return _fetch(link, workdir, limit)
    except Unavailable:
        if not os.path.exists(COOKIES):
            raise
    authdir = os.path.join(workdir, "auth")
    os.mkdir(authdir)
    jar = os.path.join(authdir, "cookies.txt")
    shutil.copyfile(COOKIES, jar)
    try:
        post = _fetch(link, authdir, limit, jar)
    except Unavailable as e:
        if e.reason == "restricted":
            log.warning("still age-restricted while logged in: the session has probably "
                        "expired (rerun ig-login.sh), or the account isn't marked as an adult")
        raise
    post.logged_in = True
    return post


def _fetch(link: Link, workdir: str, limit: int, cookies: str | None = None) -> Post:
    """One attempt at `fetch`.

    Two passes. Reading the post first, with ignore_no_formats_error, keeps the
    photo entries alive - in download mode yt-dlp raises on any entry without a
    video regardless of that option. Then only the video entries are downloaded.
    """
    opts = {
        "logger": _Quiet(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,                  # the container has no writable home
        "ignore_no_formats_error": True,    # photos: keep the metadata, see above
        # The progressive formats are H.264 + AAC in one MP4, which every
        # Discord client plays. The DASH streams are VP9 and need a merge.
        "format": "b[ext=mp4]/b/bv*+ba",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "max_filesize": MAX_SOURCE,
        "socket_timeout": 20,
        "retries": 2,
    }
    if cookies:
        opts["cookiefile"] = cookies
    post = Post()
    with yt_dlp.YoutubeDL(opts) as ydl:
        url = _resolve_share(ydl, link.url) if link.share else link.url
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            raise Unavailable(str(e).removeprefix("ERROR: ")) from None
        if not info:
            raise Unavailable("no data returned")
        entries = info.get("entries") if info.get("_type") == "playlist" else [info]
        post.author = info.get("channel") or info.get("uploader") or ""
        post.caption = info.get("description") or ""
        for i, entry in enumerate(entries or [], 1):
            if not entry:
                continue
            post.author = post.author or entry.get("channel") or ""
            if entry.get("formats"):
                try:
                    ydl.process_ie_result(entry, download=True)
                except yt_dlp.utils.DownloadError as e:
                    log.info("slide %d of %s failed: %s", i, url, e)
                    continue
                files = sorted(glob.glob(os.path.join(workdir, f"{glob.escape(entry['id'])}.*")))
                files = [f for f in files if not f.endswith((".part", ".ytdl"))]
                if files:
                    post.media.append(Media(files[0], "video"))
            elif entry.get("thumbnail"):
                data = ydl.urlopen(entry["thumbnail"]).read()
                path = os.path.join(workdir, f"{i:02d}-{entry.get('id', 'photo')}.{_sniff(data)}")
                with open(path, "wb") as f:
                    f.write(data)
                post.media.append(Media(path, "image"))

    kept = []
    for m in post.media:
        if os.path.getsize(m.path) <= limit:
            kept.append(m)
        elif m.kind == "video" and shutil.which("ffmpeg") and (small := _shrink(m.path, limit)):
            kept.append(Media(small, "video", compressed=True))
        else:
            post.too_big += 1
    post.media = kept
    if not post.media and not post.too_big:
        raise Unavailable("post had no photos or videos")
    return post


def batches(media: list[Media], limit: int, per_message: int = 10) -> list[list[Media]]:
    """Group attachments into messages: at most 10 each, total within the limit."""
    out, cur, size = [], [], 0
    for m in media:
        s = os.path.getsize(m.path)
        if cur and (len(cur) == per_message or size + s > limit):
            out.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += s
    if cur:
        out.append(cur)
    return out
