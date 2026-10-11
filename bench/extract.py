import sys, re, html, datetime as dt
from html.parser import HTMLParser
src = open(sys.argv[1], encoding='utf-8').read()
# split on message containers; author carried forward within a group
parts = re.split(r'<div id=chatlog__message-container-(\d+)', src)
author = None
start = dt.datetime.fromisoformat(sys.argv[2]).astimezone()
end = dt.datetime.fromisoformat(sys.argv[3]).astimezone()
def text(s):
    s = re.sub(r'<img[^>]*alt="?([^" >]*)"?[^>]*>', r'\1', s)
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    return html.unescape(s).strip()
for i in range(1, len(parts), 2):
    mid, body = int(parts[i]), parts[i+1]
    m = re.search(r'class=chatlog__author[^>]*title="?([^">]*)"?[^>]*>([^<]*)', body)
    if m: author = html.unescape(m.group(2) or m.group(1))
    t = dt.datetime.fromtimestamp(((mid >> 22) + 1420070400000)/1000).astimezone()
    if not (start <= t < end): continue
    c = re.search(r'class="?chatlog__content[^>]*>(.*?)</div>\s*(?:<div class="?chatlog__(?:attachment|embed|reactions|sticker)|</div>)', body, re.S)
    content = text(c.group(1)) if c else ''
    reply = re.search(r'chatlog__reply-author[^>]*>([^<]*)', body)
    att = len(re.findall(r'class="?chatlog__attachment\b', body))
    emb = re.findall(r'chatlog__embed-title[^>]*>(.*?)</div>', body, re.S)
    extra = ''
    if reply: extra += f' [reply to {html.unescape(reply.group(1))}]'
    if att: extra += f' [{att} attachment]'
    for e in emb: extra += f' [embed: {text(e)[:80]}]'
    print(f'{t:%a %H:%M} {author}{extra}: {content.replace(chr(10)," / ")}')
