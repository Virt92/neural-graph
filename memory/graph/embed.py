"""Embed memory bodies + skill bodies via sentence-transformers.

Output: memory/graph/embeddings.json
  {
    "model": "all-MiniLM-L6-v2",
    "dim": 384,
    "generated_at": "ISO",
    "items": { node_id: { "vec": [float...], "text_hash": "sha", "len": int } }
  }

Run after every memory edit (or via cron). build_graph.py can call this.
"""

import json
import hashlib
import os
import re
import sys
import time
from pathlib import Path

# Load model lazily so import is cheap when reusing
_model = None
def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
    return _model

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent  # memory/
OUT_PATH = HERE / "embeddings.json"

# Skills folders
HOME = Path.home()
SKILLS_DIRS = [
    HOME / ".claude" / "skills",
    HOME / ".claude" / "plugins" / "marketplaces",
]

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

def parse_md(path):
    """Return (description, body_text_for_embedding)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None, None
    m = FM_RE.match(raw)
    desc = ""
    body = raw
    if m:
        fm = m.group(1)
        body = m.group(2)
        for line in fm.splitlines():
            if line.lower().startswith("description:"):
                desc = line.split(":", 1)[1].strip()
                break
    # truncate body to keep embedding cheap (~ 4000 chars enough for MiniLM-256-tok)
    body = body[:4000].strip()
    combined = (desc + "\n\n" + body).strip()
    return desc, combined

def text_hash(s):
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:12]

def load_existing():
    if OUT_PATH.exists():
        try:
            return json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def collect_items():
    """Yield (node_id, text)."""
    # memory files
    for f in sorted(MEM_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        node_id = "mem_" + f.stem
        _desc, text = parse_md(f)
        if text:
            yield node_id, text
    # core skills
    for sk_dir in SKILLS_DIRS:
        if not sk_dir.exists():
            continue
        for sk_md in sk_dir.rglob("SKILL.md"):
            # node id: skill_<name>
            name = sk_md.parent.name.lower()
            name = re.sub(r"[^a-z0-9_]", "_", name)
            node_id = "skill_" + name
            _desc, text = parse_md(sk_md)
            if text:
                yield node_id, text
        # also user skills root: *.md files
        if sk_dir == HOME / ".claude" / "skills":
            for sk_md in sk_dir.glob("*.md"):
                node_id = "skill_" + sk_md.stem.lower()
                _desc, text = parse_md(sk_md)
                if text:
                    yield node_id, text

def main():
    force = "--force" in sys.argv
    existing = None if force else (load_existing() or {"items": {}})
    existing_items = existing.get("items", {}) if existing else {}

    items_to_embed = []
    keep_ids = set()
    for node_id, text in collect_items():
        h = text_hash(text)
        keep_ids.add(node_id)
        prev = existing_items.get(node_id)
        if prev and prev.get("text_hash") == h:
            continue  # cached, vector still valid
        items_to_embed.append((node_id, text, h))

    out_items = {k: v for k, v in existing_items.items() if k in keep_ids}

    if items_to_embed:
        print(f"Embedding {len(items_to_embed)} new/changed items (cached: {len(out_items)})...", flush=True)
        model = get_model()
        texts = [t for _, t, _ in items_to_embed]
        vecs = model.encode(texts, batch_size=16, show_progress_bar=False, normalize_embeddings=True)
        for (node_id, text, h), vec in zip(items_to_embed, vecs):
            out_items[node_id] = {
                "vec": [float(x) for x in vec.tolist()],
                "text_hash": h,
                "len": len(text),
            }
    else:
        print("All embeddings cached, nothing to do.", flush=True)

    payload = {
        "model": "all-MiniLM-L6-v2",
        "dim": 384,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "items": out_items,
    }
    OUT_PATH.write_text(json.dumps(payload), encoding="utf-8")
    print(f"Wrote {len(out_items)} embeddings to {OUT_PATH}", flush=True)

def query_cli():
    """Usage: py embed.py --query "your text" [--k 5]
    Outputs JSON to stdout: {results: [{id, score, len}, ...]}
    """
    args = sys.argv[1:]
    q_idx = args.index("--query")
    query_text = args[q_idx + 1]
    k = 5
    if "--k" in args:
        k = int(args[args.index("--k") + 1])
    existing = load_existing()
    if not existing or not existing.get("items"):
        print(json.dumps({"error": "no embeddings index"}))
        return
    model = get_model()
    qvec = model.encode([query_text], normalize_embeddings=True)[0]
    qlist = qvec.tolist()
    scored = []
    for nid, item in existing["items"].items():
        vec = item["vec"]
        score = sum(a * b for a, b in zip(qlist, vec))
        scored.append((score, nid, item.get("len", 0)))
    scored.sort(reverse=True)
    out = {"query": query_text[:120], "results": [
        {"id": nid, "score": round(s, 4), "len": ln}
        for s, nid, ln in scored[:k]
    ]}
    print(json.dumps(out, ensure_ascii=False))

if __name__ == "__main__":
    if "--query" in sys.argv:
        query_cli()
    else:
        main()
