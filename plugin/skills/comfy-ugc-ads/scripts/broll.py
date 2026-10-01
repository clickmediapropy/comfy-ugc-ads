#!/usr/bin/env python3
"""B-roll library index (references/broll.md). A library lives outside any one batch, e.g. runs/broll-<brand>-<who>/ (spec.json, stills/,
clips/, index.json, its own jobs.jsonl and ledger), and every ad of every batch draws from it. Local and free.

  broll.py add <lib_dir> <clip.mp4> --category C --desc "..." [--product] [--who W] [--source h3|kling|seedance] [--take N] [--id ID]
      record a reviewed clip. Refuses a clip that shows the product in a pain or failed category (product rule, SKILL.md step 3).
  broll.py list <lib_dir> [--category C] [--who W] [--no-product]
      one line per clip: id, category, seconds, product, path, description. Before the reveal, use --no-product."""
import argparse, json, pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import h3prompt, media

def load(lib): p = pathlib.Path(lib) / "index.json"; return json.loads(p.read_text()) if p.exists() else []

def add(a):
    lib, clip = pathlib.Path(a.lib), pathlib.Path(a.clip)
    if a.category not in h3prompt.BROLL: raise SystemExit(f"category must be one of {', '.join(h3prompt.BROLL)}")
    if a.product and a.category in ("pain", "failed"): raise SystemExit("REFUSED: the product never appears in pain or failed b-roll; on screen during the pain it reads as the product that failed")
    if not clip.exists(): raise SystemExit(f"missing {clip}")
    idx = load(lib); n = sum(e["category"] == a.category for e in idx) + 1
    path = str(clip.resolve().relative_to(lib.resolve())) if clip.resolve().is_relative_to(lib.resolve()) else str(clip.resolve())
    e = {"id": a.id or f"{a.category}-{n:02d}", "category": a.category, "desc": a.desc, "seconds": round(media.duration(clip), 2), "path": path,
         "product_visible": a.product, "who": a.who, "source": a.source, "take": a.take}
    if any(x["id"] == e["id"] for x in idx): raise SystemExit(f"id {e['id']} already in the index")
    idx.append(e); lib.mkdir(parents=True, exist_ok=True); (lib / "index.json").write_text(json.dumps(idx, indent=1)); print(json.dumps(e))

def show(a):
    for e in load(a.lib):
        if (a.category and e["category"] != a.category) or (a.who and e.get("who") != a.who) or (a.no_product and e["product_visible"]): continue
        print(f"{e['id']:<16} {e['category']:<12} {e['seconds']:>5.2f}s {'product' if e['product_visible'] else '-':<8} {e['path']}  {e['desc']}")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("lib"); p.add_argument("clip"); p.add_argument("--category", required=True); p.add_argument("--desc", required=True)
    p.add_argument("--product", action="store_true", help="the product is visible in the clip"); p.add_argument("--who", default="")
    p.add_argument("--source", default="h3", choices=["h3", "kling", "seedance"]); p.add_argument("--take", type=int); p.add_argument("--id")
    q = sub.add_parser("list"); q.add_argument("lib"); q.add_argument("--category"); q.add_argument("--who"); q.add_argument("--no-product", action="store_true")
    a = ap.parse_args(); add(a) if a.cmd == "add" else show(a)

if __name__ == "__main__": main()
