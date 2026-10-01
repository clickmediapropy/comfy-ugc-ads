#!/usr/bin/env python3
"""Preflight for the H3-on-Comfy-Cloud ad skill. Run before any paid call.
Checks: comfy CLI, ffmpeg/ffprobe, Comfy Cloud auth (CLI login or COMFY_API_KEY), Gemini key (OPENROUTER_API_KEY, or GEMINI_API_KEY / GOOGLE_API_KEY), and (optional) the imported 8-step LoRA of voice/product takes.
Reports presence only. NEVER prints a secret value. Exit 0 = all required items OK, 1 = something missing.
Usage: python3 scripts/preflight.py [--json]"""
import json, os, shutil, subprocess, sys

def has_env(*names):
    hit = [n for n in names if os.environ.get(n, "").strip()]
    return (True, f"set via {hit[0]} (value hidden)") if hit else (False, "none of " + ", ".join(names) + " is set")

def tool(name, ver_args):
    p = shutil.which(name)
    if not p: return False, f"{name} not on PATH"
    try:
        out = subprocess.run([p, *ver_args], capture_output=True, text=True, timeout=20)
        return out.returncode == 0, p
    except Exception as e:
        return False, f"{name} failed to run: {type(e).__name__}"

def comfy_auth():
    """Ask the CLI itself; only booleans and the subscription status are read from its answer."""
    if not shutil.which("comfy"): return False, "comfy CLI missing, cannot check auth"
    try:
        r = subprocess.run(["comfy", "--json", "cloud", "status"], capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or "{}")
    except Exception as e:
        return False, f"could not read `comfy --json cloud status` ({type(e).__name__})"
    if not d.get("ok"): return False, "Comfy Cloud not authenticated: run `comfy login` (or set COMFY_API_KEY), then re-run"
    sub = (d.get("data") or {}).get("subscription") or {}
    if not sub.get("is_active"): return False, "authenticated but no active Comfy Cloud subscription (generation needs one)"
    return True, f"authenticated, subscription {sub.get('tier', '?')}/{sub.get('status', '?')}"

def ref_turbo_lora():
    """The 8-step LoRA of voice/product takes (take_graph.TURBO['ref']) is an IMPORTED model, not in the public catalog: look for it in this workspace's assets."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import take_graph
    name, py = take_graph.TURBO["ref"], os.path.expanduser("~/.local/share/uv/tools/comfy-cli/bin/python")
    code = ("import json,sys\nfrom comfy_cli.target import resolve_target\nfrom comfy_cli.http import request_json\nt=resolve_target(where='cloud'); cur=''; hit=False\n"
            "for _ in range(20):\n    st,b=request_json(t.base_url+t.path_prefix+'/assets?include_tags=loras&limit=200'+(('&cursor='+cur) if cur else ''),t,method='GET',max_bytes=8_000_000)\n"
            "    hit=hit or any(a.get('name')==sys.argv[1] for a in b.get('assets',[]))\n    cur=b.get('next_cursor') or ''\n    if hit or not b.get('has_more') or not cur: break\nprint(json.dumps({'found':hit}))\n")
    try:
        r = subprocess.run([py, "-c", code, name], capture_output=True, text=True, timeout=120); found = json.loads(r.stdout.strip().splitlines()[-1])["found"]
    except Exception as e:
        return False, f"could not list the workspace's LoRAs ({type(e).__name__}); check Models > My Models for {name}"
    if found: return True, f"{name} is in this Comfy workspace (speech takes with a voice or packshot reference can run --turbo)"
    return False, (f"{name} is not in this workspace. It is a user-imported model (on a shared Comfy Cloud Team workspace it is there once anyone imported it). "
                   "Import it in the Comfy web UI: Models > Import > https://huggingface.co/lightx2v/Minimax-h3-Turbo/resolve/main/minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors "
                   "(type loras; Creator plan or above). Until then run those takes without --turbo (base 20 steps, about twice the cost).")

def main():
    checks = [
        ("comfy CLI", True, tool("comfy", ["--version"])),
        ("ffmpeg", True, tool("ffmpeg", ["-version"])),
        ("ffprobe", True, tool("ffprobe", ["-version"])),
        ("Comfy Cloud auth", True, comfy_auth()),
        ("Gemini key (OpenRouter or Google)", True, has_env("OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")),
        ("Turbo LoRA for voice/product takes (imported)", False, ref_turbo_lora()),
    ]
    rows = [{"check": n, "required": req, "ok": ok, "detail": d} for n, req, (ok, d) in checks]
    if "--json" in sys.argv: print(json.dumps(rows, indent=1))
    else:
        for r in rows: print(f"[{'OK' if r['ok'] else 'MISSING'}] {r['check']}: {r['detail']}")
    bad = [r["check"] for r in rows if r["required"] and not r["ok"]]
    if bad: print("\nNot ready. Missing: " + ", ".join(bad) + ". Ask the user to provide them (env vars or `comfy login`); never ask them to paste secrets into chat.", file=sys.stderr)
    else: print("\nReady: all required items present.")
    sys.exit(1 if bad else 0)

if __name__ == "__main__": main()
