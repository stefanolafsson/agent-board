# Setting up a board (instructions for Claude)

Someone has opened you in a folder and asked for an agent board, pointing you
at this engine. In the steps below, `<engine>` is the folder this file is in,
and `<folder>` is the folder they want the board in (usually your working
directory). They may not be a programmer: talk plainly, ask one question at a
time, and don't show them code unless they ask.

Read `<engine>/README.md` first. It explains boards, `board.toml`, `role.json`,
notes, and how the dispatcher fans out and joins.

## 1. Check the tools

- `uv --version`. If it's missing, tell them how to install it
  (`brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`) and wait.
- `claude` is on the PATH (it runs the agents).

## 2. Create the board

```bash
<engine>/bin/board init <folder> --human <their first name, lowercase>
```

Add `--name "..."` if the folder name doesn't make a good board name. This
creates `board.toml`, `agents/` (shared rules and an example agent), `board/`
and a `CLAUDE.md` that makes later Claude sessions in this folder the board's
lead. Read that `CLAUDE.md` now: it describes your role from here on.

## 3. Understand the job

Ask, one at a time, and skip anything they've already told you:

1. What should happen without them doing it?
2. What starts it: a note from them, a time of day, or something outside, like
   new email, a file arriving, or a website changing?
3. What should come out, and where? Notes to them on the dashboard, files in
   `board/knowledge/`, a daily list they accept or decline, or drafts in a tool
   they use?
4. Which sources and tools should the agents use? The web, or connectors they
   already have in Claude (`claude mcp list` shows them)?
5. What must the agents never do?

## 4. Design, then wait for a yes

Propose the agents in a few lines each: what each one does, which notes it
gets and sends, and which tools it needs. Draw the flow with arrows. Rules:

- **As few agents as possible.** Use one agent for linear work.
- **Split only** where work fans out into parallel pieces (one run per item),
  or where two perspectives should clash (writer and critic, with a third
  deciding).
- Use a **join** role (`"join": true`) to gather fanned-out results into one run.
- Agents never send email, publish, pay or delete. They write drafts, and the
  human acts.

Don't build until they agree.

## 5. Build

- **Each agent:** `agents/<name>/CLAUDE.md` (its job in steps: what it gets, what
  it does, which note or file it produces, and its permissions) and
  `agents/<name>/role.json`. Copy the example agent's format. Give each agent only
  the tools it needs:
  - posting notes needs `Bash` in `tools` and `Bash(post-note *)` in `allow`;
  - writing files needs `Write`/`Edit` plus rules like `Write({board}/knowledge/**)`;
  - connectors need `ToolSearch` in `tools` and each tool's exact `mcp__...` name in
    `allow`. Find the names by asking a headless run, or from the connector's tool list.

  Tell agents to pass note bodies through a quoted heredoc, as the shared rules say.
- **Remove** `agents/example/` unless they want it.
- **Seed knowledge:** anything the agents need to know about the human or the task
  goes in `board/knowledge/` (for example a profile of their interests). Ask them
  for the content and don't invent it.
- **`board.toml`:**
  - `[[button]]` for things they'll start by hand;
  - `[morning]` if something should start daily;
  - `[shortlist]` if they want a daily list to accept or decline. The agent that
    keeps it writes `board/shortlist/<date>.json` (`items` with `id`, `title`,
    `url`, `summary`, `why`), and on every run it must handle the unprocessed
    decisions in `<date>.feedback.json` and set `"processed": true`;
  - `[[service]]` for a bridge from an outside source. Write it in `bin/`, use
    `boardlib.post_note`, and put any secret it needs in `.env`.

## 6. Test small

1. `<engine>/bin/board up <folder>`.
2. Post one small test note (`<engine>/bin/post-note --to <agent> ...`, run inside
   `<folder>`). Keep it cheap: ask for one or two items, not twenty. Each run uses
   roughly $0.05 to $0.50 of their Claude plan; say so before running a whole chain.
3. Follow it in `logs/board.log`, or with `<engine>/bin/board status`. Then read the
   results and `board/activity.log`. A "denied" tool means `role.json` needs a
   rule, or the instructions asked for something they shouldn't.
4. Fix and run again until it does what they asked. Then test the whole chain once.

## 7. Hand over

Tell them, briefly:

- **Dashboard:** `http://127.0.0.1:<port>` (from `board.toml`). It shows the
  timeline, their buttons, notes to them, and the shortlist if there is one.
- **Starting the board:** `<engine>/bin/board up` in this folder. For a daily
  start, offer to create a scheduled task in the Claude app that runs
  `uv run --quiet --script <engine>/bin/board morning <folder>`. It also runs
  when they next open Claude, if the Mac was off. Background launchd jobs can't
  read `~/Documents`.
- **Asking for more:** open Claude in this folder. That session is the board's
  lead (`CLAUDE.md`).
- **Git is optional.** Don't create a repo unless they ask. If the folder is (or
  later becomes) a git repo, the watcher commits `board/` after each run.

## Don't

- Change the engine (`<engine>`). If it can't do something they need, tell them.
- Write `.claude/settings.json`. Boards don't need it.
