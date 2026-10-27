#!/usr/bin/env python3
"""SessionStart hook: tell every session what changed on the board recently.

For sessions the human opens in a board folder (not agent runs), it also adds
the board's LEAD.md: those sessions act as the board's lead.

Whatever this prints is added to the session's context. It runs for agent
runs (the watcher passes it via --settings) and for interactive sessions
opened in the research folder (.claude/settings.json).
"""

import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from boardlib import BOARD, CONFIG, HUMAN, ROOT, HUMAN_DIR, INBOX, KNOWLEDGE, NAME, PROCESSING, SHORTLIST, read_activity, read_note  # noqa: E402

RECENT_HOURS = 48


def notes_in(folder: Path) -> list[str]:
    lines = []
    for path in sorted(folder.glob("*.md"))[-10:]:
        try:
            n = read_note(path)
            lines.append(f"- {n.sender} → {n.to} [{n.type}] {n.subject or path.name}")
        except Exception:
            lines.append(f"- {path.name} (unreadable)")
    return lines


def main() -> None:
    role = os.environ.get("BOARD_ROLE")
    out = [f"# {NAME}: recent changes on the board ({BOARD})"]
    if role:
        out.append(f"You are running as the **{role}** agent.")

    cutoff = time.time() - RECENT_HOURS * 3600
    changed = sorted(
        (p for p in KNOWLEDGE.rglob("*.md") if p.stat().st_mtime > cutoff),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if changed:
        out.append(f"\n## Knowledge changed in the last {RECENT_HOURS}h")
        out += [f"- knowledge/{p.relative_to(KNOWLEDGE)} ({time.strftime('%a %H:%M', time.localtime(p.stat().st_mtime))})" for p in changed[:10]]

    activity = [e for e in read_activity(60) if e.get("event") in ("done", "error", "blocked", "delivered")][-12:]
    if activity:
        out.append("\n## Latest agent runs")
        for e in activity:
            files = f" → {', '.join(e['files'][:3])}" if e.get("files") else ""
            out.append(f"- {e['ts'][5:16]} {e.get('role')} [{e.get('event')}] {e.get('outcome', '')}{files}")

    if "shortlist" in CONFIG:
        today = SHORTLIST / f"{date.today():%Y-%m-%d}.json"
        out.append(f"\n## Today's shortlist: {'shortlist/' + today.name if today.exists() else 'not written yet'}")

    for title, folder in (("Waiting in inbox", INBOX), ("Being worked on", PROCESSING)):
        lines = notes_in(folder)
        if lines:
            out.append(f"\n## {title}")
            out += lines
    if not role:
        lines = notes_in(HUMAN_DIR)
        if lines:
            out.append(f"\n## Recent notes for {HUMAN}")
            out += lines

    out.append("\nCheck knowledge/ before redoing work another agent may already have done.")

    # A session the human opened in the board folder is the board's lead.
    lead = ROOT / "LEAD.md"
    if not role and lead.exists():
        out.append("\n---\n" + lead.read_text(encoding="utf-8").strip())
    print("\n".join(out))


if __name__ == "__main__":
    main()
