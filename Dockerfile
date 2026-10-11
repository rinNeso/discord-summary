FROM python:3.13-slim
RUN pip install --no-cache-dir "discord.py>=2.4,<3" "aiohttp>=3.9" tzdata
WORKDIR /app
COPY bot.py stats.py .
USER 568:568
CMD ["python", "-u", "bot.py"]
