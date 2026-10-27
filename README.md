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

## Start a board

Clone this repo somewhere (once), make an empty folder for your board, open
Claude in it (the desktop app's Code tab, or `claude` in a terminal), and say
what you want, for example:

> Set up an agent board here with /path/to/agent-board. I want agents that find
> new funding calls every week that fit my research, and help me draft
> applications.

Claude follows [SETUP.md](SETUP.md): it creates the board, asks what you need,
proposes the agents and waits for your yes, builds them, tests them on
something small, and shows you how to use the board. After that, any Claude
session you open in the folder is the board's lead.

## Needs

- [Claude Code](https://claude.com/claude-code), signed in (`claude` on your PATH).
- [uv](https://docs.astral.sh/uv/). `board` runs through it and gets Python 3.11+
  and `watchdog` on its own.

Put the engine's `bin/` on your PATH, so `board`, `post-note` and `open-agent`
work anywhere:

```bash
echo 'export PATH="/path/to/agent-board/bin:$PATH"' >> ~/.zshrc
```

## Commands

```bash
board init [FOLDER] [--name "My board"]   # empty folder (default: here) -> board
board up [BOARD]       # start watcher, dashboard and services in the background
board down [BOARD]     # stop them
board status [BOARD]   # running? today's runs, problems, cost, what's waiting, agents
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
  CLAUDE.md           makes a Claude session opened here the board's lead
  board.toml          name, human, port, buttons, morning note, services, shortlist
  agents/CLAUDE.md    rules every agent shares (note format, fan-out rule, guards)
  agents/<role>/      CLAUDE.md (instructions) + role.json (model, tools, permissions)
  bin/                the board's own scripts; agents can run them by name
  board/              inbox, processing, archive, <human>/, knowledge/, state/, activity.log
  .env                secrets for agents and services (ignored by git, if you use it)
  logs/               watcher log and raw output of each run (ignored by git)
```

`board init` creates this from `templates/new-board/`, with an example agent,
and picks a free dashboard port.

### board.toml

```toml
name = "Research Board"     # dashboard title
human = "stefan"            # notes to this name go to board/stefan/
port = 8787
# escalate_to = "some-agent" # failures go to you unless this names an agent

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

[shortlist]                 # optional panel: board/shortlist/<date>.json items with Accept / Decline.
                            # Decisions go to <date>.feedback.json; an agent reads them on its next run.
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

`add_dirs` gives the agent extra folders (`{root}` is the whole board folder). `tools` is what exists in the run, and `allow` is what may run without asking (anything
else is denied, since nobody is there to approve it). In `allow`, `{board}`
becomes the board folder's absolute path, e.g. `Write({board}/knowledge/**)`.
Agents that use MCP connectors need `ToolSearch` in `tools` and each tool's full
name (`mcp__...`) in `allow`.

## Talking to a board: the lead is your Claude session

Open Claude (the desktop app's Code tab, or `claude` in a terminal) in a board
folder, and that session is the board's lead. The board's `CLAUDE.md` tells it
to check `board status` and your notes first, then how to address agents
(`post-note`), explain failures, and change agents only after you agree. Agent
runs also see that file (Claude Code reads `CLAUDE.md` in parent folders), so it
opens by telling them to skip it.

Failed runs, blocked chains, notes to unknown agents and broken notes are
reported to you as `escalation` notes in `board/<human>/`, so you see them on
the dashboard and in the next session you open. To send them to an agent
instead, set `escalate_to = "<agent>"` in `board.toml`. Nothing about that agent
itself or about an escalation is escalated, so this can't loop.

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
cost, denied tools). If the board folder is a git repo (optional; `board init`
doesn't create one), `board/` is committed after each run.
Each run starts with a SessionStart hook (`engine/session_start.py`, passed by
the watcher) that tells the agent what changed on the board recently. Boards
need no `.claude/settings.json`.

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
| `SETUP.md` | The steps Claude follows to set up a new board with someone. |

The minimal teaching version for workshop participants, which works on Mac
and Windows, is a separate repo: `agent-board-kit`.
