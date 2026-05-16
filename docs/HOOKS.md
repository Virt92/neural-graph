# Hooks

Claude Code fires shell hooks at well-defined points. Neural-graph routes ALL of them through one entry point: `memory/graph/hook_handler.py`.

## Hook events used

| Event | Fires when | What handler does |
|---|---|---|
| `SessionStart` | new Claude session begins | create session entry, write `core_<sid8>` to sessions.json |
| `UserPromptSubmit` | user sends message | bump session activity ts, log prompt |
| `PreToolUse` | tool about to run | optionally block / log intent |
| `PostToolUse` | tool returned | append to traffic.jsonl with file/MCP target |
| `Stop` | Claude finishes turn | tail transcript for token delta |
| `SessionEnd` | session terminated | mark inactive (triggers 30s ghost fade in viewer) |

## Settings example

`install.ps1` merges this block into `~/.claude/settings.json` automatically. Manual format if you'd rather paste:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "py \"<absolute path to hook_handler.py>\"" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "py \"<absolute path to hook_handler.py>\"" } ] }
    ],
    "PreToolUse": [
      { "matcher": ".*", "hooks": [ { "type": "command", "command": "py \"<absolute path to hook_handler.py>\"" } ] }
    ],
    "PostToolUse": [
      { "matcher": ".*", "hooks": [ { "type": "command", "command": "py \"<absolute path to hook_handler.py>\"" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "py \"<absolute path to hook_handler.py>\"" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "py \"<absolute path to hook_handler.py>\"" } ] }
    ]
  }
}
```

## Hook payload

Claude Code passes JSON via stdin. `hook_handler.py` reads `sys.stdin`, dispatches by `hook_event_name` field. See `hook_handler.py` for exact dispatch logic.

## Cost / latency

The handler is fire-and-forget; bad input is silently dropped. No tokens consumed (it's local Python, not an LLM call). Latency budget < 50ms per call.

## Verifying hooks fire

After install, run a Claude session and check:

```powershell
type memory\graph\activity\events.log
```

Should grow as you interact. If empty:
1. `~/.claude/settings.json` missing the hooks block — re-run `install.ps1`
2. `py` not on PATH — install Python or change `py` to `python` in settings
3. Path to `hook_handler.py` wrong (Windows backslashes need escaping in JSON)
