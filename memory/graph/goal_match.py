"""Phase 8b — match a free-text user prompt to active goal(s).

CLI:
  py goal_match.py "fix map markers bug" --k 2
  py goal_match.py "deploy nginx" --json
  py goal_match.py --rebuild

Cache: memory/graph/goals_embeddings.json (per-goal, sha-invalidated by tag-set+title)
Model: sentence-transformers/all-MiniLM-L6-v2

Score = 0.6 * cos_sim + 0.3 * tag_overlap + 0.1 * priority_boost
  (priority_boost: P1=1.0, P2=0.7, P3=0.4, P4/5=0.1; only active status counts)
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
GOALS_FILE = MEM_DIR / "goals" / "active.yaml"
CACHE_PATH = HERE / "goals_embeddings.json"

PRIORITY_BOOST = {1: 1.0, 2: 0.7, 3: 0.4, 4: 0.1, 5: 0.1}

_model = None
def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
    return _model


def load_goals():
    if not GOALS_FILE.exists():
        return {}
    with open(GOALS_FILE, "r", encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    return d.get("goals") or {}


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}}


def save_cache(c):
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE_PATH)


def text_hash(s):
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]


def embed(text):
    return get_model().encode([text], normalize_embeddings=True)[0].tolist()


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def tokenize(text):
    return set(re.findall(r"[a-zа-я0-9_\.]+", text.lower()))


def collect(force=False):
    goals = load_goals()
    cache = load_cache()
    items = cache.get("items", {})
    out = []
    for gid, g in goals.items():
        title = g.get("title", "")
        why = g.get("why", "")
        tags = g.get("tags") or []
        subs = " ".join((s.get("title", "") for s in (g.get("subgoals") or [])))
        embed_input = f"{title}\n{why}\n{' '.join(tags)}\n{subs}"
        h = text_hash(embed_input)
        cached = items.get(gid)
        if not force and cached and cached.get("hash") == h:
            vec = cached["vec"]
        else:
            vec = embed(embed_input)
            items[gid] = {"hash": h, "vec": vec}
        g["_id"] = gid
        g["_vec"] = vec
        g["_tags_set"] = set(t.lower() for t in tags)
        out.append(g)
    cache["items"] = items
    cache["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_cache(cache)
    return out


def query(text, k=3, force_rebuild=False, include_paused=False):
    goals = collect(force=force_rebuild)
    if not goals:
        return []
    qvec = embed(text)
    qtoks = tokenize(text)
    scored = []
    for g in goals:
        if not include_paused and g.get("status") not in ("active",):
            continue
        sim = cos(qvec, g["_vec"])
        overlap = len(qtoks & g["_tags_set"]) / max(1, len(g["_tags_set"]))
        boost = PRIORITY_BOOST.get(g.get("priority", 5), 0.1)
        final = 0.6 * sim + 0.3 * overlap + 0.1 * boost
        scored.append({
            "goal_id": g["_id"],
            "title": g.get("title"),
            "project": g.get("project"),
            "status": g.get("status"),
            "priority": g.get("priority"),
            "progress": g.get("progress", 0),
            "similarity": round(sim, 3),
            "tag_overlap": round(overlap, 3),
            "score": round(final, 3),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-paused", action="store_true")
    args = ap.parse_args()

    if args.rebuild and not args.text:
        gs = collect(force=True)
        print(f"re-embedded {len(gs)} goals")
        return

    if not args.text:
        ap.print_help()
        sys.exit(1)

    results = query(args.text, k=args.k, force_rebuild=args.rebuild, include_paused=args.include_paused)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        print("no goals matched")
        return
    print(f"top {len(results)} goals for: {args.text!r}\n")
    for i, r in enumerate(results, 1):
        proj = r["project"] or "-"
        print(f"{i}. [{r['goal_id']}] P{r['priority']} {r['progress']:>3}%  score={r['score']}  sim={r['similarity']}  tag={r['tag_overlap']}")
        print(f"   {r['title']}    [{proj}]\n")


if __name__ == "__main__":
    main()
