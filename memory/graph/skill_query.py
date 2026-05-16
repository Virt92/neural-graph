"""Skill RAG retrieval — find most relevant learned skills for a task.

CLI:
  py skill_query.py "deploy backend to staging" --k 3
  py skill_query.py --rebuild      # force re-embed all skills

Reads: memory/skills/learned/*.md
Cache: memory/graph/skills_embeddings.json (SHA1-invalidated)
Model: sentence-transformers/all-MiniLM-L6-v2 (same as embed.py)
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
SKILLS_DIR = MEM_DIR / "skills" / "learned"
CACHE_PATH = HERE / "skills_embeddings.json"

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

_model = None
def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
    return _model


def parse_skill(path):
    """Return dict {name, description, tags, body, success_rate, uses}."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    m = FM_RE.match(raw)
    fm = {}
    body = raw
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            fm[k.strip().lower()] = v.strip()
    name = fm.get("name") or path.stem
    desc = fm.get("description", "")
    tags = fm.get("tags", "[]")
    uses = int(fm.get("uses", 0) or 0)
    success = int(fm.get("success", 0) or 0)
    fail = int(fm.get("fail", 0) or 0)
    rate = (success / max(1, success + fail)) if (success + fail) else 0.0
    return {
        "name": name, "description": desc, "tags": tags,
        "uses": uses, "success": success, "fail": fail, "success_rate": rate,
        "body": body, "path": str(path),
    }


def text_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": {}}


def save_cache(cache):
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, CACHE_PATH)


def embed_text(text):
    import numpy as np
    v = get_model().encode([text], normalize_embeddings=True)[0]
    return v.tolist()


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def collect_and_embed(force=False):
    cache = load_cache()
    items = cache.get("items", {})
    skills = []
    if not SKILLS_DIR.exists():
        return skills, cache
    for p in SKILLS_DIR.glob("*.md"):
        s = parse_skill(p)
        if not s:
            continue
        # text used for embedding: description + first 1500 chars of body
        embed_input = f"{s['description']}\n{s['body'][:1500]}"
        h = text_hash(embed_input)
        cached = items.get(s["name"])
        if not force and cached and cached.get("hash") == h:
            s["vec"] = cached["vec"]
        else:
            s["vec"] = embed_text(embed_input)
            items[s["name"]] = {"hash": h, "vec": s["vec"]}
        skills.append(s)
    cache["items"] = items
    cache["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_cache(cache)
    return skills, cache


def query(text, k=3, force_rebuild=False):
    skills, _ = collect_and_embed(force=force_rebuild)
    if not skills:
        return []
    qvec = embed_text(text)
    scored = []
    for s in skills:
        sim = cos(qvec, s["vec"])
        # mastery boost: success_rate weighted by uses (cold-start neutral 0.5)
        mastery = s["success_rate"] if (s["success"] + s["fail"]) >= 3 else 0.5
        # final score: 80% similarity + 20% mastery
        final = 0.8 * sim + 0.2 * mastery
        scored.append({
            "name": s["name"],
            "description": s["description"],
            "path": s["path"],
            "uses": s["uses"],
            "success_rate": round(s["success_rate"], 2),
            "similarity": round(sim, 3),
            "score": round(final, 3),
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="task description")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--json", action="store_true", help="output JSON instead of text")
    args = ap.parse_args()

    if args.rebuild and not args.text:
        skills, _ = collect_and_embed(force=True)
        print(f"re-embedded {len(skills)} skills")
        return

    if not args.text:
        ap.print_help()
        sys.exit(1)

    results = query(args.text, k=args.k, force_rebuild=args.rebuild)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    if not results:
        print("no skills found")
        return
    print(f"top {len(results)} skills for: {args.text!r}\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['name']}  [score={r['score']}  sim={r['similarity']}  uses={r['uses']}  win={r['success_rate']*100:.0f}%]")
        print(f"   {r['description']}")
        print(f"   {r['path']}\n")


if __name__ == "__main__":
    main()
