FROM python:3.13-slim
# ffmpeg re-encodes reels that are over the server's upload limit, and merges
# DASH streams when Instagram offers no progressive MP4. It also feeds /tts audio
# to the voice connection.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp is deliberately unpinned: Instagram breaks logged-out access every so
# often and the fix is a newer yt-dlp. Rebuild with --no-cache to pick one up.
# instaloader is only for ig_login.py: yt-dlp has no Instagram password login.
# discord.py[voice] brings PyNaCl and davey (Discord's DAVE voice encryption,
# without which voice won't connect). piper-tts is the /tts voice, run locally.
RUN pip install --no-cache-dir "discord.py[voice]>=2.7,<3" "aiohttp>=3.9" tzdata \
    "yt-dlp[default]" instaloader "piper-tts>=1.3,<2"
# Voices are ~63 MB each, fetched from Hugging Face once at build time.
RUN mkdir /opt/voices && cd /opt/voices \
    && python -m piper.download_voices en_US-lessac-medium en_US-amy-medium en_US-ryan-medium
WORKDIR /app
COPY bot.py instagram.py ig_login.py tts.py stats.py .
USER 568:568
ENV HOME=/tmp
CMD ["python", "-u", "bot.py"]
