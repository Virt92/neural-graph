"""
Live watcher: re-runs build_graph.py whenever any *.md in memory/ changes.
Polling-based (no extra deps).
Usage: py watch.py    (or launched via watch.bat)
"""
import os
import sys
import time
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
INTERVAL = 2.0  # seconds


def snapshot():
    sig = {}
    for p in MEM_DIR.glob("*.md"):
        try:
            st = p.stat()
            sig[p.name] = (st.st_mtime, st.st_size)
        except FileNotFoundError:
            pass
    return sig


def build():
    print(f"[{time.strftime('%H:%M:%S')}] rebuilding graph...")
    res = subprocess.run([sys.executable, str(HERE / "build_graph.py")], cwd=HERE)
    if res.returncode != 0:
        print(f"[{time.strftime('%H:%M:%S')}] build failed ({res.returncode})")
    else:
        print(f"[{time.strftime('%H:%M:%S')}] ok")


def main():
    print(f"watching {MEM_DIR} for *.md changes, interval={INTERVAL}s")
    build()
    prev = snapshot()
    while True:
        try:
            time.sleep(INTERVAL)
            cur = snapshot()
            if cur != prev:
                changed = [k for k in set(cur) | set(prev) if cur.get(k) != prev.get(k)]
                print(f"changed: {changed}")
                build()
                prev = cur
        except KeyboardInterrupt:
            print("stop.")
            break


if __name__ == "__main__":
    main()
