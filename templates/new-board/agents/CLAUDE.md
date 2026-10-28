# Board rules (shared by every agent)

You are one of several Claude agents that work together through a shared
board: a folder of markdown files. You were started headless by the watcher
because a note arrived for you. Nobody is watching this session live, so don't
ask questions. Do the job, hand it on, and finish.

## The board

The board folder's absolute path is in your prompt and in `$BOARD_DIR`.

| Path | What it is |
|---|---|
| `inbox/` | New notes. The watcher routes each one by its `to` field. Never write here directly; use `post-note`. |
| `processing/` | Notes currently being worked on, including yours. |
| `archive/YYYY-MM-DD/` | Handled notes. |
| `<human>/` | Notes addressed to the human who owns the board (the `human` name in `board.toml`). They show on the dashboard. |
| `knowledge/` | Durable findings that other agents and the human can reuse. |
| `activity.log` | One JSON line per event. The watcher writes it, so you don't have to. |

## Posting a note

Always pass the body on stdin with a quoted heredoc:

```
post-note --to <role|human> --type <type> --subject "<one line>" <<'EOF'
Markdown body. Headings, lists and links are fine here.
EOF
```

Don't use `--body "..."` for multi-line text: a quoted argument with a line
starting with `#` is blocked by the permission check.

Run it by its bare name, `post-note`, as a command of its own: no path in
front of it, no `cd`, and nothing chained with `;` or `&&`. Anything else is
denied, since nobody is there to approve it.

Write board files with the Write tool, not with Bash (`cat >`, `mkdir`). Write
creates missing folders, and Bash file writes are denied.

`from`, `chain` and `hop` are filled in for you. Write each note so that it
stands alone: the next agent sees only your note and the knowledge folder, not
your session. Include links, ids and the facts it needs.

## When to split work (the fan-out rule)

- If the work is linear, do it yourself. You may use subagents inside your own
  run.
- Post several notes only when the work truly fans out into independent
  parallel pieces, such as one note per paper or one per department. Each note
  starts its own agent run.
- When two perspectives should clash, send the same item to two different roles
  and let a third resolve it. Don't do another role's job yourself.

## Guards

- Don't post a note back to the role that just sent you one unless something
  is genuinely missing. The watcher stops chains after a few hand-offs.
- Post at most 10 notes per run.
- Only write to the folders your role allows. If a tool is denied, work
  around it or report it in your outcome; don't keep retrying.

## Escalating

If you're blocked, missing a permission, or something needs a decision you
can't make, post a note to the human (type `escalation`) saying what happened
and what you need. Then finish. Don't keep retrying. Failed runs are reported
to the human automatically.

## Finishing

End with the structured result: `outcome` (one line, under 120 characters,
saying what you did, e.g. "Posted 6 candidates to reviewer") and `files` (board
files you created or changed, relative to the board folder).
