"""Shared helpers for a board: where it is, its config, the note format, the activity log.

A board is any folder with a board.toml, an agents/ folder and a board/
folder. The engine (this repo) works on whichever board it's pointed at.

A note is a markdown file with a short header:

    ---
    to: reviewer
    from: searcher
    type: candidate
    created: 2026-10-27T06:00:12
    chain: 20261027-060000-ab12
    hop: 1
    subject: Some paper title
    ---
    Body in markdown.

The watcher routes on `to`. `chain` ties together every note that came out of
one triggering event, and `hop` counts hand-offs along the chain (loop guard).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    raise SystemExit("agent-board needs Python 3.11 or newer.") from None

ENGINE = Path(__file__).resolve().parent.parent  # the agent-board repo


def find_root() -> Path:
    """The board folder: $BOARD_ROOT, or the nearest folder above the current one with a board.toml."""
    if os.environ.get("BOARD_ROOT"):
        return Path(os.environ["BOARD_ROOT"]).expanduser().resolve()
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        if (folder / "board.toml").exists():
            return folder
    raise SystemExit("No board here: run this inside a board folder (one with a board.toml), or set BOARD_ROOT.")


ROOT = find_root()
CONFIG = tomllib.loads((ROOT / "board.toml").read_text(encoding="utf-8")) if (ROOT / "board.toml").exists() else {}
NAME = CONFIG.get("name", ROOT.name)
HUMAN = CONFIG.get("human", "human").lower()  # notes to this name go to board/<human>/
PORT = int(os.environ.get("BOARD_PORT", CONFIG.get("port", 8787)))

BOARD = ROOT / "board"
INBOX = BOARD / "inbox"
PROCESSING = BOARD / "processing"
ARCHIVE = BOARD / "archive"
HUMAN_DIR = BOARD / HUMAN
SHORTLIST = BOARD / "shortlist"
KNOWLEDGE = BOARD / "knowledge"
STATE = BOARD / "state"
TMP = BOARD / ".tmp"
ACTIVITY_LOG = BOARD / "activity.log"
AGENTS = ROOT / "agents"
LOGS = ROOT / "logs"

HEADER_KEYS = ("to", "from", "type", "created", "chain", "hop", "subject")

_log_lock = threading.Lock()


def child_env(**extra: str) -> dict:
    """Environment for anything the engine starts (agents, bridges, tools).

    PATH finds the engine's tools, the board's own bin/ and this Python first,
    so `post-note` and board scripts run with the same interpreter, and
    PYTHONPATH lets them `import boardlib`.
    """
    import sys
    paths = [ENGINE / "bin", ROOT / "bin", Path(sys.executable).parent]
    return {
        **os.environ,
        "BOARD_ROOT": str(ROOT),
        "BOARD_DIR": str(BOARD),
        "PATH": os.pathsep.join([*map(str, paths), os.environ.get("PATH", "")]),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ENGINE / "engine"), os.environ.get("PYTHONPATH", "")])),
        **extra,
    }


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def new_chain_id() -> str:
    return f"{now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "note"


@dataclass
class Note:
    to: str
    sender: str
    type: str
    body: str
    subject: str = ""
    created: str = field(default_factory=lambda: now().isoformat())
    chain: str = field(default_factory=new_chain_id)
    hop: int = 0
    path: Path | None = None

    def render(self) -> str:
        header = {
            "to": self.to,
            "from": self.sender,
            "type": self.type,
            "created": self.created,
            "chain": self.chain,
            "hop": self.hop,
        }
        if self.subject:
            header["subject"] = self.subject.replace("\n", " ")
        lines = ["---", *(f"{k}: {v}" for k, v in header.items()), "---", ""]
        return "\n".join(lines) + self.body.rstrip() + "\n"

    def filename(self) -> str:
        stamp = self.created.replace("-", "").replace(":", "").replace("T", "-")[:15]
        label = slugify(self.subject or self.type, 30)
        return f"{stamp}-{self.sender}-to-{self.to}-{label}-{secrets.token_hex(2)}.md"

    def summary(self) -> dict:
        return {
            "id": self.path.name if self.path else None,
            "to": self.to,
            "from": self.sender,
            "type": self.type,
            "subject": self.subject,
            "created": self.created,
            "chain": self.chain,
            "hop": self.hop,
        }


class NoteError(ValueError):
    pass


def parse_note(text: str, path: Path | None = None) -> Note:
    if not text.startswith("---"):
        raise NoteError("missing header (file must start with ---)")
    try:
        _, header, body = text.split("---", 2)
    except ValueError:
        raise NoteError("header is not closed with ---") from None
    fields: dict[str, str] = {}
    for line in header.strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    for key in ("to", "from", "type"):
        if not fields.get(key):
            raise NoteError(f"header is missing `{key}`")
    try:
        hop = int(fields.get("hop", 0))
    except ValueError:
        hop = 0
    return Note(
        to=fields["to"].lower(),
        sender=fields["from"].lower(),
        type=fields["type"],
        body=body.lstrip("\n"),
        subject=fields.get("subject", ""),
        created=fields.get("created") or now().isoformat(),
        chain=fields.get("chain") or new_chain_id(),
        hop=hop,
        path=path,
    )


def read_note(path: Path) -> Note:
    return parse_note(path.read_text(encoding="utf-8"), path)


def post_note(note: Note) -> Path:
    """Write a note into the inbox atomically, so the watcher never sees half a file."""
    TMP.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    tmp = TMP / note.filename()
    tmp.write_text(note.render(), encoding="utf-8")
    final = INBOX / tmp.name
    os.replace(tmp, final)
    note.path = final
    return final


def log_activity(**entry) -> dict:
    """Append one JSON line to activity.log. Keys: role, event, trigger, outcome, files, ..."""
    entry = {"ts": now().isoformat(), **entry}
    with _log_lock, ACTIVITY_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_activity(limit: int | None = None) -> list[dict]:
    if not ACTIVITY_LOG.exists():
        return []
    entries = []
    for line in ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries[-limit:] if limit else entries


SESSIONS = STATE / "sessions.json"
_sessions_lock = threading.Lock()


def load_sessions() -> dict:
    """Resident roles' sessions: {role: {"current": {...}, "previous": [...]}}."""
    return json.loads(SESSIONS.read_text(encoding="utf-8")) if SESSIONS.exists() else {}


def _save_sessions(data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    SESSIONS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def current_session(role: str, replace: bool = False) -> dict:
    """This month's session for a resident role. A new month (or replace=True) starts a new one.

    The returned dict has `id`, `month`, `created` and `runs` (0 means the
    session doesn't exist yet and must be created with --session-id).
    """
    month = f"{date.today():%Y-%m}"
    with _sessions_lock:
        data = load_sessions()
        entry = data.setdefault(role, {"current": None, "previous": []})
        current = entry.get("current")
        if current and current["month"] == month and not replace:
            return current
        if current and current.get("runs"):
            entry["previous"].append(current)
        entry["current"] = {"id": str(uuid.uuid4()), "month": month, "created": now().isoformat(), "runs": 0}
        _save_sessions(data)
        return entry["current"]


def record_session_run(role: str, session_id: str) -> None:
    with _sessions_lock:
        data = load_sessions()
        current = data.get(role, {}).get("current")
        if current and current["id"] == session_id:
            current["runs"] = current.get("runs", 0) + 1
            current["last_run"] = now().isoformat()
            _save_sessions(data)


def list_roles() -> dict[str, dict]:
    """Every folder in agents/ with a role.json is a role."""
    roles = {}
    for role_file in sorted(AGENTS.glob("*/role.json")):
        roles[role_file.parent.name] = json.loads(role_file.read_text(encoding="utf-8"))
    return roles
