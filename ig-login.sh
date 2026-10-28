#!/bin/bash
# Logs the bot's Instagram account in so reposts work for age-restricted posts
# and private accounts it follows. Only the session cookies are saved; the
# password goes straight into the container's hidden prompt - no history, argv
# or file. Delete the cookies file to go back to logged-out only.
set -euo pipefail
d=/mnt/apps/discord-summary/instagram
install -d -m 700 -o 568 -g 568 "$d"
docker run --rm -it -v "$d:/out" discord-summary:local python ig_login.py
