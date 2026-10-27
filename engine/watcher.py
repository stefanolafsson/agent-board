"""Watcher and dispatcher for the board.

Watches board/inbox/. When a note arrives, the dispatcher decides what to do
with it. It routes and splits work but never does the work itself:

- `to: <human>`       -> moved to board/<human>/ for the dashboard
- `to: <role>`        -> one headless `claude -p` run per note, so several notes
                         to the same role fan out in parallel
- join roles          -> roles with "join": true in role.json (e.g. the curator)
                         wait until every other run in the same chain has
                         finished, then get all their notes in one run
- resident roles      -> roles with "resident": true keep one persistent
                         session per month and run one at a time; later
                         notes wait in the inbox until the role is free
- loop guards         -> notes past MAX_HOPS hand-offs, or chains past
                         MAX_RUNS_PER_CHAIN runs, are archived as blocked

Every start and finish goes to board/activity.log, and after each run the
board folder is committed to git, if it's a git repo.

Started by `board up` or `board run` (see bin/board).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boardlib import (  # noqa: E402
    AGENTS, ARCHIVE, BOARD, HUMAN, HUMAN_DIR, INBOX, LOGS, PROCESSING, ROOT,
    CONFIG, Note, NoteError, child_env, post_note, current_session, list_roles, log_activity, now, read_activity, read_note,
    record_session_run,
)

MAX_HOPS = int(os.environ.get("BOARD_MAX_HOPS", 6))
MAX_RUNS_PER_CHAIN = int(os.environ.get("BOARD_MAX_RUNS_PER_CHAIN", 40))
MAX_PARALLEL = int(os.environ.get("BOARD_MAX_PARALLEL", 4))
HOOK = Path(__file__).resolve().parent / "session_start.py"
ESCALATE_TO = CONFIG.get("escalate_to", HUMAN)       # who hears about failures: the human, or an agent
ESCALATE_DENIED = CONFIG.get("escalate_denied", False)  # also tell it about denied tools?
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "description": "One line, under 120 characters: what this run did."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Board files you created or changed (paths relative to the board folder). Notes posted with post-note do not need to be listed.",
        },
    },
    "required": ["outcome", "files"],
}

# SessionStart hook for agent runs, so each one starts with the board's recent changes.
AGENT_SETTINGS = {
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": f'"{sys.executable}" "{HOOK}"'}]}]
    }
}


def say(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def archive(path: Path, reason: str | None = None) -> Path:
    dest_dir = ARCHIVE / f"{date.today():%Y-%m-%d}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    os.replace(path, dest)
    return dest


def rel(path: Path) -> str:
    return str(path.relative_to(BOARD))


class Dispatcher:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.git_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(MAX_PARALLEL)
        self.active: Counter[str] = Counter()  # chain -> runs queued or running
        self.runs: Counter[str] = Counter(  # chain -> runs started, survives restarts
            e["chain"] for e in read_activity() if e.get("event") == "start" and e.get("chain")
        )
        self.timers: dict[tuple[str, str], threading.Timer] = {}
        self.busy: set[str] = set()  # resident roles with a run in progress

    # ---- routing -------------------------------------------------------

    def scan(self) -> None:
        with self.lock:
            roles = list_roles()
            joins: dict[tuple[str, str], list[Note]] = defaultdict(list)
            for path in sorted(INBOX.glob("*.md")):
                note = self._read(path)
                if note is None:
                    continue
                if note.to == HUMAN:
                    dest = HUMAN_DIR / path.name
                    os.replace(path, dest)
                    log_activity(role=HUMAN, event="delivered", chain=note.chain, hop=note.hop,
                                 trigger=note.sender, outcome=note.subject or note.type, files=[rel(dest)])
                elif note.to not in roles:
                    self._reject(note, "unrouted", f"no agent called '{note.to}'")
                elif note.hop > MAX_HOPS:
                    self._reject(note, "blocked", f"hop limit ({MAX_HOPS}) reached")
                elif self.runs[note.chain] >= MAX_RUNS_PER_CHAIN:
                    self._reject(note, "blocked", f"chain run limit ({MAX_RUNS_PER_CHAIN}) reached")
                elif roles[note.to].get("join"):
                    joins[(note.to, note.chain)].append(note)
                elif note.to in self.busy:
                    continue  # resident role is working; rescanned when it finishes
                else:
                    self._launch(note.to, roles[note.to], [note])

            # Join roles run once per chain, after everything upstream in the chain is done.
            for (role, chain), notes in joins.items():
                if self.active[chain] or role in self.busy:
                    continue  # rescanned when the chain's last job (or the resident role) finishes
                settle = roles[role].get("settle_s", 0)
                youngest = min(time.time() - n.path.stat().st_mtime for n in notes)
                if youngest < settle:
                    self._rescan_later((role, chain), settle - youngest + 0.5)
                    continue
                self._launch(role, roles[role], notes)

    def _read(self, path: Path) -> Note | None:
        try:
            return read_note(path)
        except FileNotFoundError:
            return None
        except (NoteError, UnicodeDecodeError) as exc:
            # A file dropped in by hand may still be half-written; give it a moment.
            if time.time() - path.stat().st_mtime < 10:
                self._rescan_later(("invalid", path.name), 10)
                return None
            dest = archive(path)
            escalate(log_activity(role="dispatcher", event="invalid", trigger=path.name, outcome=str(exc),
                                  files=[rel(dest)]))
            say(f"invalid note {path.name}: {exc}")
            return None

    def _reject(self, note: Note, event: str, reason: str) -> None:
        dest = archive(note.path)
        escalate(log_activity(role=note.to, event=event, chain=note.chain, hop=note.hop,
                              trigger=f"{note.sender}:{note.type}", outcome=reason, files=[rel(dest)]))
        say(f"{event}: {note.path.name} ({reason})")

    def _rescan_later(self, key: tuple[str, str], delay: float) -> None:
        if key in self.timers and self.timers[key].is_alive():
            return
        timer = threading.Timer(delay, self.scan)
        timer.daemon = True
        self.timers[key] = timer
        timer.start()

    # ---- running -------------------------------------------------------

    def _launch(self, role: str, cfg: dict, notes: list[Note]) -> None:
        claimed = []
        for note in notes:
            dest = PROCESSING / note.path.name
            try:
                os.replace(note.path, dest)
            except FileNotFoundError:
                continue
            note.path = dest
            claimed.append(note)
        if not claimed:
            return
        chain = claimed[0].chain
        if cfg.get("resident"):
            self.busy.add(role)
        self.active[chain] += 1
        self.runs[chain] += 1
        run_id = f"{now():%Y%m%d-%H%M%S}-{role}-{os.urandom(2).hex()}"
        trigger = ", ".join(sorted({f"{n.sender}:{n.type}" for n in claimed}))
        log_activity(role=role, event="start", run=run_id, chain=chain, hop=max(n.hop for n in claimed),
                     trigger=trigger, notes=[n.path.name for n in claimed],
                     outcome=claimed[0].subject if len(claimed) == 1 else f"{len(claimed)} notes")
        say(f"start {role} ({trigger}, {len(claimed)} note(s), chain {chain})")
        self.pool.submit(self._run, role, cfg, claimed, run_id, chain, trigger)

    def _run(self, role: str, cfg: dict, notes: list[Note], run_id: str, chain: str, trigger: str) -> None:
        started = time.time()
        try:
            result = run_agent(role, cfg, notes, run_id)
        except Exception as exc:  # never let one run take the watcher down
            result = {"event": "error", "outcome": f"watcher error: {exc}", "files": []}
        archived = [rel(archive(n.path)) for n in notes if n.path.exists()]
        entry = log_activity(role=role, run=run_id, chain=chain, hop=max(n.hop for n in notes), trigger=trigger,
                             duration_s=round(time.time() - started), notes=archived, **result)
        say(f"{entry['event']} {role}: {entry['outcome']}")
        if entry["event"] == "error" or (ESCALATE_DENIED and entry.get("denied")):
            escalate(entry)
        self._commit(f"[{role}] {entry['outcome']}"[:200])
        with self.lock:
            self.active[chain] -= 1
            self.busy.discard(role)
        self.scan()

    def _commit(self, message: str) -> None:
        if not (ROOT / ".git").exists():
            return
        with self.git_lock:
            subprocess.run(["git", "add", "-A", "board"], cwd=ROOT, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", message, "--", "board"], cwd=ROOT, capture_output=True)

    def recover(self) -> None:
        """Notes left in processing/ by a crash go back to the inbox."""
        for path in PROCESSING.glob("*.md"):
            os.replace(path, INBOX / path.name)
            say(f"recovered {path.name}")


def escalate(entry: dict) -> None:
    """Post a note about a failed run, blocked chain or bad note to the human (or escalate_to agent).

    It starts a new chain, and nothing about the escalation agent itself or about
    an escalation note is escalated, so escalations can't loop.
    """
    to_agent = ESCALATE_TO != HUMAN
    if not ESCALATE_TO or (to_agent and ESCALATE_TO not in list_roles()) or entry.get("role") == ESCALATE_TO:
        return
    if "escalation" in (entry.get("trigger") or ""):
        return
    facts = [
        f"- what happened: {entry['event']}",
        f"- agent: {entry.get('role')}",
        f"- triggered by: {entry.get('trigger')}",
        f"- outcome: {entry.get('outcome')}",
        f"- chain: {entry.get('chain', '-')}",
    ]
    if entry.get("denied"):
        facts.append(f"- denied tools: {', '.join(entry['denied'])}")
    if entry.get("run"):
        facts.append(f"- raw output of the run: logs/runs/{entry['run']}.json")
    facts += [f"- file: board/{f}" for f in entry.get("files", []) + entry.get("notes", [])]
    next_step = ("Find out why, fix what you're allowed to fix, and tell the human if it needs a decision."
                 if to_agent else "Open a Claude session in the board folder and ask it to look into this.")
    body = "\n".join(["The watcher reports a problem on the board.", "", *facts, "", next_step])
    post_note(Note(to=ESCALATE_TO, sender="dispatcher", type="escalation",
                   subject=f"{entry['event']}: {entry.get('role')}: {str(entry.get('outcome', ''))[:70]}", body=body))


def build_prompt(role: str, notes: list[Note], run_id: str) -> str:
    parts = [
        f"You are the **{role}** agent on the shared board.",
        f"Board folder: {BOARD}",
        f"The human who owns this board is `{HUMAN}`: send them notes with `post-note --to {HUMAN}`.",
        f"Today is {date.today():%A %Y-%m-%d}. Run id: {run_id}.",
        "",
        f"You were triggered by {len(notes)} note(s). They are in board/processing/ and will be archived after this run.",
    ]
    for i, note in enumerate(notes, 1):
        parts += ["", f"=== note {i}: {note.path.name} ===", note.path.read_text(encoding="utf-8").strip()]
    parts += [
        "",
        "Do your job as your CLAUDE.md describes. Hand work on with `post-note`.",
        "Finish with the structured result: a one-line outcome and the board files you created or changed.",
    ]
    return "\n".join(parts)


def resident_args(role: str, session: dict) -> list[str]:
    if session["runs"]:
        return ["--resume", session["id"]]
    return ["--session-id", session["id"], "--name", f"board:{role} ({session['month']})"]


def run_agent(role: str, cfg: dict, notes: list[Note], run_id: str) -> dict:
    # In role.json, {board} and {root} stand for the board/ folder and the whole board folder.
    # Permission rules use //path for absolute paths.
    def expand(text: str, rule: bool) -> str:
        prefix = "/" if rule else ""
        return text.replace("{board}", prefix + str(BOARD)).replace("{root}", prefix + str(ROOT))

    allowed = [expand(rule, True) for rule in cfg.get("allow", [])]
    cmd = [
        CLAUDE, "-p",
        "--model", cfg.get("model", "sonnet"),
        "--tools", ",".join(cfg.get("tools", ["Read", "Glob", "Grep"])),
        "--allowedTools", ",".join(allowed),
        "--add-dir", str(BOARD), *(expand(d, False) for d in cfg.get("add_dirs", [])),
        "--output-format", "json",
        "--json-schema", json.dumps(RESULT_SCHEMA),
        "--max-budget-usd", str(cfg.get("max_budget_usd", 1.0)),
        "--settings", json.dumps(AGENT_SETTINGS),
    ]
    if cfg.get("effort"):
        cmd += ["--effort", cfg["effort"]]
    env = child_env(
        BOARD_ROLE=role,
        BOARD_RUN=run_id,
        BOARD_CHAIN=notes[0].chain,
        BOARD_HOP=str(max(n.hop for n in notes)),
    )
    prompt = build_prompt(role, notes, run_id)

    def call(session_args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd + session_args, input=prompt, cwd=AGENTS / role, env=env,
                              capture_output=True, text=True, timeout=cfg.get("timeout_s", 900))

    session = None
    if cfg.get("resident"):
        # One persistent session per month: create it on the first run, resume it after that.
        session = current_session(role)
        proc = call(resident_args(role, session))
        output = proc.stdout + proc.stderr
        if session["runs"] and "No conversation found" in output:
            session = current_session(role, replace=True)  # the session is gone; start this month's again
            proc = call(resident_args(role, session))
        elif not session["runs"] and "is already in use" in output:
            session = {**session, "runs": 1}  # created by a run that never got recorded; resume it
            proc = call(resident_args(role, session))
    else:
        proc = call(["--name", f"board:{role}:{run_id}"])
    (LOGS / "runs").mkdir(parents=True, exist_ok=True)
    (LOGS / "runs" / f"{run_id}.json").write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        return {"event": "error", "outcome": f"claude exited {proc.returncode}: {tail[0][:200]}", "files": []}

    if session and data.get("session_id") == session["id"]:
        record_session_run(role, session["id"])
    structured = data.get("structured_output") or {}
    outcome = structured.get("outcome") or (data.get("result") or "").strip().splitlines()[0:1] or ["(no outcome)"]
    if isinstance(outcome, list):
        outcome = outcome[0]
    return {
        "event": "error" if data.get("is_error") else "done",
        "outcome": outcome[:300],
        "files": structured.get("files", []),
        "cost_usd": round(data.get("total_cost_usd") or 0, 4),
        "session": data.get("session_id"),
        "denied": [d.get("tool_name") for d in data.get("permission_denials", [])],
    }


class InboxHandler(FileSystemEventHandler):
    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        self.pending: threading.Timer | None = None
        self.lock = threading.Lock()

    def on_any_event(self, event) -> None:
        if event.is_directory or event.event_type not in ("created", "moved", "modified", "closed"):
            return
        path = getattr(event, "dest_path", "") or event.src_path
        if not str(path).endswith(".md"):
            return
        # Debounce: a burst of notes (fan-out) becomes one scan.
        with self.lock:
            if self.pending:
                self.pending.cancel()
            self.pending = threading.Timer(0.3, self.dispatcher.scan)
            self.pending.daemon = True
            self.pending.start()


def main() -> None:
    for folder in (INBOX, PROCESSING, ARCHIVE, HUMAN_DIR, LOGS):
        folder.mkdir(parents=True, exist_ok=True)
    dispatcher = Dispatcher()
    dispatcher.recover()
    observer = Observer()
    observer.schedule(InboxHandler(dispatcher), str(INBOX), recursive=False)
    observer.start()
    roles = ", ".join(list_roles()) or "none"
    say(f"watching {INBOX} (roles: {roles}; max {MAX_PARALLEL} parallel, {MAX_HOPS} hops)")
    log_activity(role="dispatcher", event="online", outcome=f"watcher started; roles: {roles}")
    dispatcher.scan()  # anything that arrived while the watcher was down
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        say("stopping (running agents finish first)")
        observer.stop()
        dispatcher.pool.shutdown(wait=True)
        observer.join()


if __name__ == "__main__":
    main()
