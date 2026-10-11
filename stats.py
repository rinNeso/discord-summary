"""Usage stats for the /summary bot: docker exec discord-summary python stats.py"""
import collections, json, os
path = os.environ.get("USAGE_LOG", "/data/usage.jsonl")
try:
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
except FileNotFoundError:
    rows = []
if not rows:
    raise SystemExit("no /summary uses recorded yet")
ok = [r for r in rows if r["outcome"] == "ok"]
print(f"{len(rows)} uses since {rows[0]['ts'][:16]}, {len(ok)} succeeded")
for title, key in [("by outcome", "outcome"), ("by user", "user"), ("by server", "guild"),
                   ("by channel", "channel"), ("by day", lambda r: r["ts"][:10])]:
    c = collections.Counter(key(r) if callable(key) else r.get(key) for r in rows)
    print(f"\n{title}:"); [print(f"  {n:4}  {k}") for k, n in c.most_common()]
if ok:
    secs = sorted(r["secs"] for r in ok); msgs = sorted(r.get("messages", 0) for r in ok)
    print(f"\nsuccessful runs: median {secs[len(secs)//2]}s, max {secs[-1]}s; "
          f"median {msgs[len(msgs)//2]} msgs, max {msgs[-1]}")
    print(f"big model: {sum(r['big'] for r in ok)}, public: {sum(r['public'] for r in ok)}, "
          f"truncated: {sum(1 for r in ok if r.get('dropped'))}")
