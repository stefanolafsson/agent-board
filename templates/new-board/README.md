# A board

Agents that work together through notes. Run with the agent-board engine:

    board up      # start (dashboard address is printed)
    board down    # stop

- `board.toml`: name, port, quick buttons, morning note, services.
- `agents/<name>/`: one folder per agent, with `CLAUDE.md` (instructions) and
  `role.json` (model, tools, permissions, cost cap).
- `agents/CLAUDE.md`: rules every agent shares.
- `LEAD.md`: open Claude (app or CLI) in this folder and that session is the
  board's lead: you tell it what you want, and it addresses the agents, checks
  status and changes agents with your OK.
- `board/`: the notes, knowledge and activity log.
- `bin/`: this board's own scripts. Agents can run them by name.
