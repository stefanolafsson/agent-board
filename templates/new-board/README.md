# A board

Agents that work together through notes. Run with the agent-board engine:

    board up      # start (dashboard address is printed)
    board down    # stop

- `board.toml`: name, port, quick buttons, morning note, services.
- `agents/<name>/`: one folder per agent, with `CLAUDE.md` (instructions) and
  `role.json` (model, tools, permissions, cost cap).
- `agents/CLAUDE.md`: rules every agent shares.
- `board/`: the notes, knowledge and activity log.
- `bin/`: this board's own scripts. Agents can run them by name.
