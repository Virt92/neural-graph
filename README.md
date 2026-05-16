# Neural Graph

Self-observability layer for [Claude Code](https://github.com/anthropics/claude-code).

A live 3D visualization of your Claude sessions, projects, skills, MCP servers, decisions, goals, and the **traffic** flowing between them. Hooks capture every tool call. Background daemons score health, growth, anomalies, causality. Nightly job builds the graph and writes daily digests.

> **What it is:** a skeleton — install on a fresh machine, start working with Claude, watch your own neural graph accumulate over time.
>
> **What it isn't:** a backup of someone else's data. No personal memory shipped.

---

## What you get

- **Electron 3D graph** (`memory/graph/index.html`) — projects, skills, MCPs as glowing nodes; sessions as warm-white core ghosts; live edges pulse on tool use.
- **3 view modes** — TOPOLOGY (static structure), ACTIVITY (default — hot edges only), TRAFFIC (animated tubes per file/MCP event).
- **Per-session core agents** — every Claude session you launch becomes its own labeled core ghost. 30s decay on session end.
- **Snake animation** — green dot travels source→target while edge is hot, retracts target→source over 30s when cold.
- **Position persistence** — drag a project, position survives restart.
- **Project filter** — isolate one project's view.
- **Legend panel** — collapsible bottom panel explaining every color/status/daemon (with reassurance about token costs).
- **Hook-driven data capture** — `hook_handler.py` runs on every SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / SessionEnd.
- **Background daemons** — process_watcher (5s), infra (30s), health (1h), growth (4h), signals (6h).
- **Nightly batch** — reflect → decisions → baselines → anomalies → embed → build_graph → health → growth → signals → curiosity → gaps → causality → snapshot.

---

## Requirements

| Tool | Why |
|---|---|
| **[Claude Code](https://github.com/anthropics/claude-code)** | the agent whose work you're observing |
| **Python 3.10+** | hook handler, daemons, graph builder |
| **Node 18+** | Electron viewer |
| **Git** | clone + pull updates |

Optional auto-installed via `requirements.txt`: `psutil`, `PyYAML`.

---

## Install (fresh machine)

```powershell
git clone https://github.com/Virt92/neural-graph.git
cd neural-graph
powershell -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` does:
1. `npm install` in `memory/graph/electron/`
2. `pip install -r requirements.txt`
3. Merges hook entries into `~/.claude/settings.json` (preserves existing config)
4. Creates Windows Task Scheduler job `NeuralGraphNightly` at 03:13 daily
5. Creates desktop shortcut to `launch.bat`
6. Prints next steps

After install:

```powershell
.\launch.bat
```

Electron opens. Empty graph. Start a Claude session in any project — your first core ghost appears within seconds.

---

## How it works

```
┌──────────────────┐     hooks      ┌────────────────────┐
│ Claude Code CLI  │ ─────────────► │  hook_handler.py   │
└──────────────────┘                └─────────┬──────────┘
                                              │ writes
                                              ▼
                                    ┌──────────────────────┐
                                    │ activity/sessions    │
                                    │   .json / traffic    │
                                    │   .jsonl / events    │
                                    └─────────┬────────────┘
                                              │ poll 250ms
                                              ▼
        ┌─────────────────────────────────────────────────┐
        │  Electron viewer (index.html + 3d-force-graph)  │
        │   - session core ghosts                         │
        │   - hot edges (decay)                           │
        │   - snake animation forward/retract             │
        │   - project filter, position persistence        │
        └─────────────────────────────────────────────────┘
                          ▲
                          │ graph_data.js
                          │
                  ┌───────────────┐  daily 03:13
                  │  nightly.bat  │ -- Task Scheduler
                  └───────────────┘
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the data model.

---

## Manual commands (rare)

`launch.bat` is enough for normal use. These are one-off operations:

| Command | Use case |
|---|---|
| `py memory/graph/build_graph.py` | rebuild graph now (don't wait for nightly) |
| `py memory/graph/goals.py status <gid> done` | close a goal |
| `py memory/graph/scratchpad.py working "msg"` | soft-lock for parallel sessions |
| `py memory/graph/snapshot.py write` | snapshot graph state |

---

## Personal memory

The skeleton ships **empty**. Your memory accumulates here as you work:

- `memory/MEMORY.md` — index Claude maintains
- `memory/project_*.md` — ongoing projects
- `memory/feedback_*.md` — your corrections to Claude
- `memory/user_*.md` — your profile, preferences
- `memory/reference_*.md` — pointers to dashboards/docs

`.gitignore` excludes all of these. Each machine = its own memory. Use a separate private repo or cloud sync if you want a backup of personal `.md` files.

---

## Dev rules

If you (or future Claude sessions) work on this codebase, read [`docs/DEV_RULES.md`](docs/DEV_RULES.md) first. Hard-won lessons:
- Additive changes only — never break existing modes
- Per-session core, not global
- World-Y projection for curve direction (NOT cross product)
- Hot-edge gating to prevent stale-session noise
- Don't redesign without confirming

---

## License

MIT — see [LICENSE](LICENSE).

---

## Credits

Built collaboratively with Claude Code. Originated by [@Virt92](https://github.com/Virt92).
