# A board

Agents that work together through notes. Run with the agent-board engine:

    board up      # start (dashboard address is printed)
    board down    # stop

- `board.toml`: name, port, quick buttons, morning note, services.
- `agents/<name>/`: one folder per agent, with `CLAUDE.md` (instructions) and
  `role.json` (model, tools, permissions, cost cap).
- `agents/CLAUDE.md`: rules every agent shares.
- `agents/lead/`: your front desk. Talk to it through the dashboard's "Post a
  note" box or `open-agent lead`. It addresses the other agents, reports status,
  handles failures, and proposes changes to agents for you to approve.
- `board/`: the notes, knowledge and activity log.
- `bin/`: this board's own scripts. Agents can run them by name.
