#!/usr/bin/env python3
"""Cost ledger for Comfy Cloud jobs. Parameterized by a work dir.
Every paid submit is recorded (failures included) in <work>/jobs.jsonl; `build` matches them to Comfy Cloud billing events
(GET /api/billing/events, the Plan & Credits > Activity feed) and writes <work>/ledger.json. The CLI balance cannot measure spend on TEAM plans.
  ledger.py record <work> <name> <prompt_id> [kind]   append a job (call right after every submit)
  ledger.py build  <work>                              rebuild ledger.json, print totals
  ledger.py check  <work> <est_usd> <cap_usd>          exit 1 if spent + est > cap (spend guard)"""
import json, pathlib, shutil, subprocess, sys, time

RATE_UI = 0.27313   # credits per GPU-second, conservative (implied by Comfy UI vs events, 2026-09-25); docs rate is 0.266
CR_PER_USD = 211

_FETCH = r'''
import json, sys
from comfy_cli.target import resolve_target
from comfy_cli.http import request_json
t = resolve_target(where="cloud"); out = []; page = 1
while True:
    st, b = request_json(t.base_url + t.path_prefix + f"/billing/events?limit=100&page={page}", t, max_bytes=20_000_000)
    out += b["events"]
    if page >= b.get("totalPages", 1): break
    page += 1
print(json.dumps(out))
'''

def comfy_python():
    """The interpreter of the comfy CLI install (it holds comfy_cli, which resolves the OAuth/API-key credential; nothing is printed)."""
    exe = shutil.which("comfy")
    if not exe: raise SystemExit("comfy CLI not found")
    line = open(exe, "rb").readline().decode(errors="ignore").strip()
    return line[2:].strip().split()[0] if line.startswith("#!") else sys.executable

def events():
    r = subprocess.run([comfy_python(), "-c", _FETCH], capture_output=True, text=True)
    if r.returncode: raise SystemExit("could not read billing events: " + r.stderr.strip().splitlines()[-1][:200])
    return json.loads(r.stdout)

def record(work, name, prompt_id, kind="job"):
    work = pathlib.Path(work); work.mkdir(parents=True, exist_ok=True)
    with open(work / "jobs.jsonl", "a") as f:
        f.write(json.dumps({"name": name, "kind": kind, "prompt_id": prompt_id, "submitted": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")

def build(work, ev=None):
    work = pathlib.Path(work); jp = work / "jobs.jsonl"
    jobs = [json.loads(l) for l in open(jp)] if jp.exists() else []
    by_job = {}
    for e in (events() if ev is None else ev):
        jid = e.get("params", {}).get("job_id")
        if jid: by_job.setdefault(jid, []).append(e)
    rows, tot = [], 0.0
    for j in jobs:
        es = by_job.get(j["prompt_id"], [])
        api = sum(e["params"].get("credits_used", 0) for e in es if e["event_type"] == "api_usage_completed")
        gpu = sum(e["params"].get("gpu_seconds", 0) for e in es if e["event_type"] == "cloud_workflow_executed")
        cr = api + gpu * RATE_UI; tot += cr
        rows.append({**j, "billed": bool(es), "api_credits": round(api, 4), "gpu_seconds": round(gpu, 3), "credits": round(cr, 4), "usd": round(cr / CR_PER_USD, 4)})
    led = {"source": "Comfy Cloud /api/billing/events matched by job_id", "credits_per_usd": CR_PER_USD, "gpu_rate_credits_per_s": RATE_UI,
           "jobs": rows, "total_credits": round(tot, 3), "total_usd": round(tot / CR_PER_USD, 4),
           "unbilled_jobs": [r["prompt_id"] for r in rows if not r["billed"]],
           "note": "billing events lag; unbilled_jobs may still be pending. Re-run build before quoting the final total."}
    json.dump(led, open(work / "ledger.json", "w"), indent=1)
    return led

if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 4 and a[0] == "record": record(a[1], a[2], a[3], a[4] if len(a) > 4 else "job")
    elif len(a) == 2 and a[0] == "build":
        l = build(a[1]); print(json.dumps({k: l[k] for k in ("total_credits", "total_usd", "unbilled_jobs")}))
    elif len(a) == 4 and a[0] == "check":
        spent = build(a[1])["total_usd"]; est, cap = float(a[2]), float(a[3])
        print(f"spent ${spent:.2f} + est ${est:.2f} vs cap ${cap:.2f}")
        sys.exit(1 if spent + est > cap else 0)
    else: sys.exit(__doc__)
