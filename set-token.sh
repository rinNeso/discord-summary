#!/bin/bash
# Writes the bot token without it touching shell history, argv, or a transcript.
set -euo pipefail
f=/mnt/apps/discord-summary/secrets/.env
read -rsp "Discord bot token (hidden): " T; echo
read -rp  "Server (guild) ID, blank to skip: " G
umask 077
{ echo "DISCORD_TOKEN=$T"; [[ -n $G ]] && echo "GUILD_ID=$G"; } > "$f"
chmod 600 "$f"; chown root:root "$f"
echo "saved to $f"
cd /mnt/apps/discord-summary/stack && docker compose up -d && sleep 5 && docker logs --tail 5 discord-summary
