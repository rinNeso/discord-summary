import json, sys, time, urllib.request, re, collections
SP = sys.argv[1]
URL = "http://127.0.0.1:11434/api/chat"
PROMPT = open(f"{SP}/prompt.txt").read()
DAY_PROMPT = PROMPT.replace("Stay under 1500 characters.", "Stay under 900 characters.")
MERGE = """Below are per-day summaries of a Discord group chat covering one week. Combine them into one summary for someone who missed the whole week.

Rules:
- Group by topic; a topic that spans several days is one entry. Short bullets, plain language.
- Say who said what, using the names as written. Only use facts present in the day summaries.
- Keep jokes and banter as what they were; don't sanitise, moralise or editorialise.
- Start with one line: total messages, the date span, and who was talking.
- Stay under 1500 characters. No preamble, no closing offer.

Day summaries:
{days}"""
def chat(model, content, ctx):
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "stream": False, "think": False, "options": {"num_ctx": ctx}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    d = json.load(urllib.request.urlopen(req, timeout=3600))
    st = dict(wall=round(time.time()-t,1), load=round(d.get("load_duration",0)/1e9,1),
              prompt_tok=d.get("prompt_eval_count"), prompt_s=round(d.get("prompt_eval_duration",0)/1e9,1),
              out_tok=d.get("eval_count"), out_s=round(d.get("eval_duration",0)/1e9,1))
    return re.sub(r"<think>.*?</think>", "", d["message"]["content"], flags=re.S).strip(), st
lines = open(f"{SP}/week.txt").read().splitlines()
days = collections.OrderedDict()
for l in lines:
    key = l[:3]
    # Sun appears twice (Oct 4 and Oct 11): split on first return to a seen day after others
    if days and key in days and list(days)[-1] != key: key += "2"
    days.setdefault(key, []).append(l)
for model in sys.argv[2:]:
    tag = model.split(":")[1].split("-")[0]
    out, st = chat(model, PROMPT.format(tz="America/Chicago", transcript="\n".join(lines)), 65536)
    open(f"{SP}/out_single_{tag}.txt","w").write(out); print(model, "single", st, flush=True)
    parts, tot = [], dict(wall=0, prompt_tok=0, out_tok=0)
    for k, ls in days.items():
        o, s = chat(model, DAY_PROMPT.format(tz="America/Chicago", transcript="\n".join(ls)), 32768)
        parts.append(f"## {k} ({len(ls)} messages)\n{o}")
        for f in tot: tot[f] += s[f] or 0
        print(model, "day", k, s, flush=True)
    o, s = chat(model, MERGE.format(days="\n\n".join(parts)), 32768)
    for f in tot: tot[f] += s[f] or 0
    open(f"{SP}/out_days_{tag}.txt","w").write("\n\n".join(parts))
    open(f"{SP}/out_mr_{tag}.txt","w").write(o); print(model, "mapreduce total", tot, flush=True)
