"""Decision RAG retrieval — find past decisions relevant to a topic.

CLI:
  py decisions_query.py "which DB for memory store"
  py decisions_query.py "deploy strategy" --k 5 --project proj_example
  py decisions_query.py --rebuild

Reads: memory/graph/activity/decisions.jsonl
Cache: memory/graph/decisions_embeddings.json (per-id)
Model: sentence-transformers/all-MiniLM-L6-v2
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACT_DIR = HERE / "activity"
SRC_PATH = ACT_DIR / "decisions.jsonl"
CACHE_PATH = HERE / "decisions_embeddings.json"

_model = None
def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
    return _model


def load_decisions():
    if not SRC_PATH.exists():
        return []
    out = []
    for ln in SRC_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}}


def save_cache(cache):
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE_PATH)


def embed_text(text):
    v = get_model().encode([text], normalize_embeddings=True)[0]
    return v.tolist()


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def collect_and_embed(force=False):
    decisions = load_decisions()
    cache = load_cache()
    items = cache.get("items", {})
    out = []
    for d in decisions:
        did = d.get("id")
        if not did:
            continue
        text = d.get("decision_text") or ""
        ctx = d.get("context_before") or ""
        embed_input = f"{text}\n{ctx[:300]}"
        cached = items.get(did)
        if not force and cached:
            d["vec"] = cached["vec"]
        else:
            d["vec"] = embed_text(embed_input)
            items[did] = {"vec": d["vec"]}
        out.append(d)
    cache["items"] = items
    cache["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_cache(cache)
    return out


def query(text, k=5, project=None, force_rebuild=False):
    decisions = collect_and_embed(force=force_rebuild)
    if not decisions:
        return []
    if project:
        decisions = [d for d in decisions if d.get("project_id") == project]
        if not decisions:
            return []
    qvec = embed_text(text)
    scored = []
    for d in decisions:
        sim = cos(qvec, d["vec"])
        # outcome boost: success +0.05, fail -0.05, partial 0
        outcome = (d.get("outcome") or "").lower()
        bonus = 0.05 if outcome == "success" else (-0.05 if outcome == "fail" else 0.0)
        scored.append({
            "id": d["id"],
            "date": d.get("date"),
            "project_id": d.get("project_id"),
            "speaker": d.get("speaker"),
            "decision_text": d.get("decision_text"),
            "context_before": d.get("context_before"),
            "outcome": d.get("outcome"),
            "outcome_note": d.get("outcome_note"),
            "match_pattern": d.get("match_pattern"),
            "session_id": d.get("session_id"),
            "similarity": round(sim, 3),
            "score": round(sim + bonus, 3),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="topic to search past decisions for")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--project", help="filter by project_id (e.g. proj_example)")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.rebuild and not args.text:
        decs = collect_and_embed(force=True)
        print(f"re-embedded {len(decs)} decisions")
        return

    if not args.text:
        ap.print_help()
        sys.exit(1)

    results = query(args.text, k=args.k, project=args.project, force_rebuild=args.rebuild)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        print("no decisions found")
        return
    print(f"top {len(results)} decisions for: {args.text!r}\n")
    for i, r in enumerate(results, 1):
        outcome_tag = f" [{r['outcome']}]" if r['outcome'] else ""
        proj = r['project_id'] or '?'
        print(f"{i}. [{r['date']}] [{proj}] sim={r['similarity']}{outcome_tag}")
        print(f"   {r['decision_text'][:200]}")
        if r['context_before']:
            print(f"   ctx: {r['context_before'][:140]}")
        print()


if __name__ == "__main__":
    main()
