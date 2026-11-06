#!/usr/bin/env python3
"""Local dashboard for a board: live timeline, quick buttons, notes for the
human, and (when the board enables it) a shortlist with Accept / Decline.

Started by `board up` / `board run`. Standard library only; localhost only.
The title, port, human's name, buttons and shortlist come from board.toml.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from boardlib import (  # noqa: E402
    ACTIVITY_LOG, BOARD, CONFIG, ENGINE, HUMAN, HUMAN_DIR, INBOX, NAME, PORT, PROCESSING, ROOT, SHORTLIST,
    Note, NoteError, child_env, list_roles, load_sessions, now, post_note, read_activity, read_note,
)

PAGE = Path(__file__).resolve().parent / "dashboard.html"
BUTTONS = CONFIG.get("button", [])        # [[button]] tables: quick notes posted with one click
SHORTLIST_CONFIG = "shortlist" in CONFIG  # a [shortlist] table shows the panel
# Only the dashboard's own page may use it: other websites open in the browser
# must not be able to press buttons or (through DNS rebinding) read the board.
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")  # shortlist dates; anything else could point outside the board
_feedback_lock = threading.Lock()


def note_list(folder: Path, limit: int, with_body: bool = False) -> list[dict]:
    notes = []
    for path in sorted(folder.glob("*.md"), reverse=True)[:limit]:
        try:
            n = read_note(path)
        except Exception:
            continue
        item = n.summary()
        if with_body:
            item["body"] = n.body
        notes.append(item)
    return notes


def shortlist(day: str | None) -> dict:
    dates = sorted((p.stem for p in SHORTLIST.glob("????-??-??.json") if DAY.fullmatch(p.stem)), reverse=True)
    if not (day and DAY.fullmatch(day)):
        day = dates[0] if dates else f"{date.today():%Y-%m-%d}"
    path = SHORTLIST / f"{day}.json"
    data, error, feedback = None, None, {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            error = f"shortlist file is not valid JSON: {exc}"
    try:
        with _feedback_lock:
            feedback = reconcile_feedback(day)
    except FeedbackError as exc:
        error = error or str(exc)
    return {"date": day, "dates": dates, "data": data, "error": error, "feedback": feedback}


# Decisions: <day>.feedback.json is what the shortlist agent reads, and it rewrites
# the file to mark decisions processed. If it rewrites from an older copy while the
# human clicks, a click can be lost. Every click is therefore also appended to
# <day>.clicks.jsonl, and any click missing from feedback.json is put back.

class FeedbackError(Exception):
    pass


def feedback_path(day: str) -> Path:
    return SHORTLIST / f"{day}.feedback.json"


def clicks_path(day: str) -> Path:
    return SHORTLIST / f"{day}.clicks.jsonl"


def load_feedback(day: str) -> dict:
    path = feedback_path(day)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeedbackError(f"{path.name} is not valid JSON ({exc}); fix or delete it to record decisions") from None
    if not isinstance(data, dict):
        raise FeedbackError(f"{path.name} should hold an object of decisions")
    return data


def save_feedback(day: str, feedback: dict) -> None:
    SHORTLIST.mkdir(parents=True, exist_ok=True)
    tmp = feedback_path(day).with_suffix(".tmp")
    tmp.write_text(json.dumps(feedback, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, feedback_path(day))  # the agent never reads half a file


def reconcile_feedback(day: str) -> dict:
    """feedback.json with every click applied that it's missing. Call with _feedback_lock held."""
    feedback = load_feedback(day)
    latest: dict[str, dict] = {}
    if clicks_path(day).exists():
        for line in clicks_path(day).read_text(encoding="utf-8").splitlines():
            try:
                click = json.loads(line)
                if isinstance(click, dict) and isinstance(click.get("id"), str):
                    latest[click["id"]] = click
            except json.JSONDecodeError:
                continue
    changed = False
    for item_id, click in latest.items():
        if not (isinstance(click.get("at"), str) and click.get("decision") in ("accepted", "declined", None)):
            continue  # a damaged line; skip it rather than break the dashboard
        entry = feedback.get(item_id)
        if entry is not None and not isinstance(entry, dict):
            entry = None
        if click["decision"] is None:
            if entry and str(entry.get("at", "")) <= click["at"]:
                del feedback[item_id]
                changed = True
        elif not entry or str(entry.get("at", "")) < click["at"]:
            feedback[item_id] = {"decision": click["decision"], "title": click.get("title", ""),
                                 "at": click["at"], "processed": False}
            changed = True
    if changed:
        save_feedback(day, feedback)
    return feedback


def record_click(day: str, item_id: str, decision: str | None, title: str) -> None:
    with _feedback_lock:
        load_feedback(day)  # refuse to touch a broken file: the agent's processed flags are in it
        SHORTLIST.mkdir(parents=True, exist_ok=True)
        click = {"id": item_id, "decision": decision, "title": title, "at": datetime.now().isoformat(timespec="microseconds")}
        with clicks_path(day).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(click, ensure_ascii=False) + "\n")
        reconcile_feedback(day)


def state(query: dict) -> dict:
    return {
        "now": now().isoformat(),
        "activity": read_activity(400),
        "roles": {name: cfg.get("description", "") for name, cfg in list_roles().items()},
        "resident": resident_sessions(),
        "inbox": note_list(INBOX, 50),
        "processing": note_list(PROCESSING, 50),
        "human_notes": note_list(HUMAN_DIR, 20, with_body=True),
        "shortlist": shortlist(query.get("date", [None])[0]) if SHORTLIST_CONFIG else None,
        "board": {"name": NAME, "human": HUMAN},
        "buttons": [{"label": b["label"], "title": b.get("title", ""), "confirm": b.get("confirm", "")}
                    for b in BUTTONS],
    }


def resident_sessions() -> dict:
    sessions = load_sessions()
    return {name: (sessions.get(name) or {}).get("current")
            for name, cfg in list_roles().items() if cfg.get("resident")}


def fingerprint() -> tuple:
    """Changes whenever something the dashboard shows changes."""
    parts = []
    for p in (ACTIVITY_LOG, INBOX, PROCESSING, HUMAN_DIR, SHORTLIST):
        try:
            parts.append(p.stat().st_mtime_ns)
        except FileNotFoundError:
            parts.append(0)
    return tuple(parts)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _foreign(self, post: bool) -> bool:
        """Refuse requests that don't come from the dashboard's own page (and say so)."""
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        ok = host in ALLOWED_HOSTS and (origin is None or origin == f"http://{host}")
        # A JSON content type means a browser asks first (CORS preflight), which this server never allows.
        if post and not self.headers.get("Content-Type", "").startswith("application/json"):
            ok = False
        if not ok:
            self._json({"error": "only the dashboard's own page can use this"}, 403)
        return not ok

    def do_GET(self):
        if self._foreign(post=False):
            return
        url = urlparse(self.path)
        if url.path == "/":
            # Second line of defence against anything that slips through the markdown
            # renderer: only the page's own script runs (no inline handlers), and the
            # page can't load or send anything to another site.
            nonce = secrets.token_urlsafe(16)
            body = PAGE.read_text(encoding="utf-8").replace("<script>", f'<script nonce="{nonce}">', 1).encode()
            self.send_response(200)
            self.send_header("Content-Security-Policy", (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/state":
            self._json(state(parse_qs(url.query)))
        elif url.path == "/api/file":
            # Read-only view of a board file linked from the timeline.
            rel_path = parse_qs(url.query).get("path", [""])[0]
            target = (BOARD / rel_path).resolve()
            if BOARD.resolve() not in target.parents or not target.is_file():
                return self._json({"error": "not found"}, 404)
            self._json({"path": rel_path, "text": target.read_text(encoding="utf-8", errors="replace")})
        elif url.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = None
            try:
                while True:
                    current = fingerprint()
                    if current != last:
                        self.wfile.write(b"event: change\ndata: {}\n\n")
                        self.wfile.flush()
                        last = current
                    else:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self._foreign(post=True):
            return
        url = urlparse(self.path)
        try:
            data = self._body()
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)

        if url.path == "/api/open-session":
            # Opens a resident agent's persistent session in the Claude desktop app.
            role = data.get("role", "")
            session = resident_sessions().get(role)
            if not session or not session.get("runs"):
                return self._json({"error": f"{role} has no session yet"}, 400)
            # open-agent knows how to hand the session to the app from a background process.
            subprocess.Popen([sys.executable, str(ENGINE / "bin" / "open-agent"), role], env=child_env(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._json({"ok": True})

        if url.path == "/api/button":
            # A quick note configured in board.toml; the body can come from a file in the board.
            try:
                button = BUTTONS[int(data.get("index", -1))]
            except (ValueError, IndexError):
                return self._json({"error": "no such button"}, 400)
            body = button.get("body", "")
            if button.get("body_file"):
                body = (ROOT / button["body_file"]).read_text(encoding="utf-8")
            try:
                path = post_note(Note(to=button["to"], sender=button.get("from", HUMAN), type=button.get("type", "request"),
                                      subject=button.get("subject", button["label"]), body=body))
            except NoteError as exc:
                return self._json({"error": f"this button in board.toml is invalid: {exc}"}, 400)
            return self._json({"ok": True, "note": path.name, "to": button["to"]})

        if url.path == "/api/feedback" and SHORTLIST_CONFIG:
            # One Accept / Decline per item, saved as it's clicked. The agent that keeps
            # the shortlist reads decisions it hasn't processed yet on its next run.
            day = data.get("date") or f"{date.today():%Y-%m-%d}"
            item_id = data.get("id")
            if not DAY.fullmatch(str(day)):
                return self._json({"error": "date must look like 2026-01-31"}, 400)
            if not item_id or not isinstance(item_id, str) or data.get("decision") not in ("accepted", "declined", None):
                return self._json({"error": "send an id and a decision (accepted, declined or null)"}, 400)
            try:
                record_click(day, item_id, data.get("decision"), str(data.get("title", "")))
            except FeedbackError as exc:
                return self._json({"error": str(exc)}, 409)
            return self._json({"ok": True})

        self._json({"error": "not found"}, 404)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"{NAME} dashboard on http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
