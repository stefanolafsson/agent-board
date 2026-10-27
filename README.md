# agent-board

An engine for boards of collaborating Claude Code agents. A **board** is a
folder with agents and a shared set of markdown notes. A note in the inbox
starts the agent it's addressed to, headless; agents hand work on by posting
notes to each other. Nothing polls. This repo is the engine; each topic gets
its own board folder.

```
 dashboard / post-note / bridge / scheduled task
                    │
                    ▼
            board/inbox/  ──►  watcher  ──►  claude -p in agents/<role>/
            (a note = .md)    (dispatcher)        │
                    ▲                             ├──► board/knowledge/
                    └──────── post-note ──────────┤
                                                  └──► board/<human>/
```

Boards that use it: `~/Documents/github/research` (literature pipeline) and
`~/Documents/github/hraunbjorg-board` (workshop demo).

## Needs

- [Claude Code](https://claude.com/claude-code), signed in (`claude` on your PATH).
- [uv](https://docs.astral.sh/uv/). `board` runs through it and gets Python 3.11+
  and `watchdog` on its own.

Put the engine's `bin/` on your PATH, so `board`, `post-note` and `open-agent`
work anywhere:

```bash
echo 'export PATH="$HOME/Documents/github/agent-board/bin:$PATH"' >> ~/.zshrc
```

## Commands

```bash
board init ~/Documents/github/my-board --name "My board" --port 8790
board up [BOARD]       # start watcher, dashboard and services in the background
board down [BOARD]     # stop them
board status [BOARD]   # running? today's runs, problems, cost, open proposals, agents
board run [BOARD]      # same as up, but in the foreground (Ctrl-C stops)
board morning [BOARD]  # up, then post the board's [morning] note once per day
```

BOARD defaults to the current folder (or the nearest one above it with a
`board.toml`). Each board has its own port, so several can run at once.

```bash
post-note --to <role> --type request --subject "..." --body "..."   # inside a board folder
open-agent <role>        # open a resident agent's session in the Claude app
```

## A board

```
my-board/
  board.toml          name, human, port, buttons, morning note, services, shortlist
  agents/CLAUDE.md    rules every agent shares (note format, fan-out rule, guards)
  agents/<role>/      CLAUDE.md (instructions) + role.json (model, tools, permissions)
  bin/                the board's own scripts; agents can run them by name
  board/              inbox, processing, archive, <human>/, knowledge/, state/, activity.log
  .env                secrets for agents and services (not in git)
  logs/               watcher log and raw output of each run (not in git)
```

`board init` creates this from `templates/new-board/`, with an example agent.

### board.toml

```toml
name = "Research Board"     # dashboard title
human = "stefan"            # notes to this name go to board/stefan/
port = 8787
front_desk = "lead"         # default recipient in the dashboard's note box
escalate_to = "lead"        # who hears about failed runs and blocked notes

[[button]]                  # quick buttons on the dashboard, one note each
label = "Run morning search"
to = "searcher"
type = "morning-search"
body = "Run the daily literature search."   # or body_file = "demo/note.md"; optional from, confirm, title

[morning]                   # what `board morning` posts once a day
to = "searcher"
type = "morning-search"
body = "Run the daily literature search."

[[service]]                 # programs that run next to the watcher, e.g. bridges
name = "intranet bridge"
command = ["python3", "bin/intranet-bridge"]   # "python3" = the engine's Python
requires_env = "HRAUNBJORG_MCP_URL"            # skipped unless set (e.g. in .env)

[shortlist]                 # optional daily-shortlist panel with feedback buttons
feedback_to = "curator"
```

### role.json

```json
{
  "description": "What this agent does (shown on the dashboard).",
  "model": "sonnet",
  "tools": ["Read", "Glob", "Grep", "Bash", "WebFetch"],
  "allow": ["Read", "Glob", "Grep", "WebFetch", "Bash(post-note *)"],
  "max_budget_usd": 1.0,
  "timeout_s": 900,
  "join": false,
  "resident": false,
  "add_dirs": []
}
```

`add_dirs` gives the agent extra folders (`{root}` is the whole board folder; the lead uses it to read `agents/`). `tools` is what exists in the run, and `allow` is what may run without asking (anything
else is denied, since nobody is there to approve it). In `allow`, `{board}`
becomes the board folder's absolute path, e.g. `Write({board}/knowledge/**)`.
Agents that use MCP connectors need `ToolSearch` in `tools` and each tool's full
name (`mcp__...`) in `allow`.

## The lead agent (front desk)

New boards come with `agents/lead/`, a resident agent you talk to instead of
addressing specialists yourself:

- **Requests:** tell it what you want (dashboard "Post a note", which defaults to
  it, or `open-agent lead` in the Claude app). It posts the right notes to the
  right agents.
- **Status:** it runs `board status` and reports runs, problems, cost and what's waiting.
- **Escalations:** with `escalate_to = "lead"` in `board.toml`, the watcher sends
  it a note about every failed run, blocked chain, unknown recipient or broken
  note (and denied tools, with `escalate_denied = true`). Nothing about the lead
  itself or about an escalation is escalated, so this can't loop.
- **Proposals:** it never changes agents on its own. It writes
  `board/proposals/<date>-<slug>.md` (why, exact change, risk) and asks you. When
  you answer "approve <file>" in a note or in the app, it applies exactly that
  change. Its edits to `agents/` aren't auto-committed, so `git diff` shows them.

The dispatcher stays plain code: the lead is where you talk, not a router every
note passes through.

## Notes

```markdown
---
to: reviewer
from: searcher
type: candidate
created: 2026-10-27T06:00:12
chain: 20261027-060000-ab12
hop: 1
subject: Paper title
---
Body in markdown.
```

`chain` groups every note that came from one starting note; `hop` counts
hand-offs. `post-note` fills both in from the run's environment. Agents should
pass multi-line bodies on stdin with a quoted heredoc: Claude Code's permission
check blocks quoted arguments that contain a line starting with `#`.

## How the dispatcher decides

- `to: <human>`: the note goes to `board/<human>/` and shows on the dashboard.
- `to: <role>`: one `claude -p` run per note, so five notes to a role become five
  parallel runs (at most `BOARD_MAX_PARALLEL`, default 4, at once).
- **Join roles** (`"join": true`) wait until every other run in the chain has
  finished, then get all their notes in one run. `settle_s` adds a quiet period.
- **Resident roles** (`"resident": true`) keep one persistent session per month
  that every run resumes, and run one at a time (later notes wait). Open the
  session in the Claude app with `open-agent <role>` or the dashboard's **Open in
  Claude**. The copy open in the app doesn't refresh itself; reopen it to see
  new runs. History isn't memory: durable findings still belong in `knowledge/`.
- **Loop guards**: a note past `BOARD_MAX_HOPS` (6) hand-offs, or a chain past
  `BOARD_MAX_RUNS_PER_CHAIN` (40) runs, is archived as `blocked`. Unknown roles
  are `unrouted`; files without a valid header are `invalid`.

Every run is logged to `board/activity.log` (time, role, trigger, outcome, files,
cost, denied tools), and the board folder is committed to git after each run.
Each run starts with a SessionStart hook (`engine/session_start.py`) that tells
the agent what changed on the board recently. Boards also wire it into
`.claude/settings.json` for interactive sessions.

## Starting boards automatically

On macOS, launchd jobs can't read `~/Documents`. Instead, use a scheduled task
in the Claude app that runs `board morning <board>` (or `board up <board>`).
Scheduled tasks run while the app is open, and a run missed while the app was
closed happens on next launch.

## Files

| Path | What it is |
|---|---|
| `bin/board` | The CLI above. |
| `bin/post-note`, `bin/open-agent` | Post a note; open a resident session. |
| `engine/boardlib.py` | Finding the board, board.toml, note format, activity log, sessions. |
| `engine/watcher.py` | Watcher and dispatcher. |
| `engine/dashboard.py`, `dashboard.html` | The dashboard (standard library only). |
| `engine/session_start.py` | The SessionStart hook. |
| `templates/new-board/` | What `board init` copies. |

The minimal teaching version for workshop participants, which works on Mac
and Windows, is a separate repo: `agent-board-kit`.
