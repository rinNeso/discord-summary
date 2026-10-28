"""Usage stats for the bot: docker exec discord-summary python stats.py"""
import collections, json, os
path = os.environ.get("USAGE_LOG", "/data/usage.jsonl")
try:
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
except FileNotFoundError:
    rows = []
if not rows:
    raise SystemExit("no uses recorded yet")


def breakdown(rows, keys):
    for title, key in keys:
        c = collections.Counter(key(r) if callable(key) else r.get(key) for r in rows)
        print(f"\n{title}:"); [print(f"  {n:4}  {k}") for k, n in c.most_common()]


# Rows from before the Instagram feature have no "kind": they're all summaries.
summaries = [r for r in rows if r.get("kind", "summary") == "summary"]
reposts = [r for r in rows if r.get("kind") == "instagram"]
speech = [r for r in rows if r.get("kind") == "tts"]

if summaries:
    ok = [r for r in summaries if r["outcome"] == "ok"]
    print(f"/summary: {len(summaries)} uses since {summaries[0]['ts'][:16]}, {len(ok)} succeeded")
    breakdown(summaries, [("by outcome", "outcome"), ("by user", "user"), ("by server", "guild"),
                          ("by channel", "channel"), ("by day", lambda r: r["ts"][:10])])
    if ok:
        secs = sorted(r["secs"] for r in ok); msgs = sorted(r.get("messages", 0) for r in ok)
        print(f"\nsuccessful runs: median {secs[len(secs)//2]}s, max {secs[-1]}s; "
              f"median {msgs[len(msgs)//2]} msgs, max {msgs[-1]}")
        print(f"big model: {sum(r['big'] for r in ok)}, public: {sum(r['public'] for r in ok)}, "
              f"truncated: {sum(1 for r in ok if r.get('dropped'))}")

if reposts:
    ok = [r for r in reposts if r["outcome"] == "ok"]
    print(f"\n{'=' * 40}\nInstagram reposts: {len(reposts)} links since {reposts[0]['ts'][:16]}, "
          f"{len(ok)} reposted")
    breakdown(reposts, [("by outcome", lambda r: r["outcome"] + (f" ({r['reason']})" if "reason" in r else "")),
                        ("by user", "user"), ("by server", "guild")])
    if ok:
        print(f"\nreposted {sum(r.get('items', 0) for r in ok)} files "
              f"({sum(r.get('videos', 0) for r in ok)} videos, "
              f"{sum(r.get('compressed', 0) for r in ok)} re-encoded to fit), "
              f"{sum(r.get('mb', 0) for r in ok):.0f} MB total, "
              f"{sum(1 for r in ok if r.get('logged_in'))} needed the login")

if speech:
    print(f"\n{'=' * 40}\n/tts: {len(speech)} sessions since {speech[0]['ts'][:16]}, "
          f"{sum(r.get('messages', 0) for r in speech)} messages read aloud, "
          f"{sum(r.get('secs', 0) for r in speech) / 3600:.1f} h in voice")
    breakdown(speech, [("by outcome", "outcome"), ("by user", "user"), ("by server", "guild"),
                       ("by voice", "voice")])
