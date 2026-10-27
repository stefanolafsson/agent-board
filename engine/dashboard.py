#!/usr/bin/env python3
"""Local dashboard for a board: live timeline, quick buttons, post a note, and
(when the board enables it) a shortlist with feedback.

Started by `board up` / `board run`. Standard library only; localhost only.
The title, port, human's name, buttons and shortlist come from board.toml.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from boardlib import (  # noqa: E402
    ACTIVITY_LOG, BOARD, CONFIG, ENGINE, HUMAN, HUMAN_DIR, INBOX, NAME, PORT, PROCESSING, ROOT, SHORTLIST,
    Note, child_env, list_roles, load_sessions, now, post_note, read_activity, read_note,
)

PAGE = Path(__file__).resolve().parent / "dashboard.html"
BUTTONS = CONFIG.get("button", [])        # [[button]] tables: quick notes posted with one click
SHORTLIST_CONFIG = CONFIG.get("shortlist")  # [shortlist] table, or absent to hide the panel


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
    dates = sorted((p.stem for p in SHORTLIST.glob("????-??-??.json")), reverse=True)
    day = day or (dates[0] if dates else f"{date.today():%Y-%m-%d}")
    path = SHORTLIST / f"{day}.json"
    data, error = None, None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            error = f"shortlist file is not valid JSON: {exc}"
    return {"date": day, "dates": dates, "data": data, "error": error, "feedback": load_feedback(day)}


def feedback_path(day: str) -> Path:
    return SHORTLIST / f"{day}.feedback.json"


def load_feedback(day: str) -> dict:
    path = feedback_path(day)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


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
        "board": {"name": NAME, "human": HUMAN, "front_desk": CONFIG.get("front_desk")},
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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
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
        url = urlparse(self.path)
        try:
            data = self._body()
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)

        if url.path == "/api/note":
            to = (data.get("to") or "").strip().lower()
            if not to or not (data.get("body") or data.get("subject")):
                return self._json({"error": "a note needs `to` and a subject or body"}, 400)
            path = post_note(Note(to=to, sender=HUMAN, type=(data.get("type") or "request").strip(),
                                  subject=(data.get("subject") or "").strip(), body=data.get("body") or ""))
            return self._json({"ok": True, "note": path.name})

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
            path = post_note(Note(to=button["to"], sender=button.get("from", HUMAN), type=button.get("type", "request"),
                                  subject=button.get("subject", button["label"]), body=body))
            return self._json({"ok": True, "note": path.name, "to": button["to"]})

        if url.path == "/api/feedback" and SHORTLIST_CONFIG:
            # Marks are saved as you click; "send" posts the unsent ones to the curator as one note.
            day = data.get("date") or f"{date.today():%Y-%m-%d}"
            feedback = load_feedback(day)
            for item_id, mark in (data.get("marks") or {}).items():
                feedback[item_id] = {**feedback.get(item_id, {}), **mark, "sent": False}
            note_name = None
            if data.get("send"):
                unsent = {k: v for k, v in feedback.items() if not v.get("sent")}
                if not unsent:
                    return self._json({"error": "no new feedback to send"}, 400)
                lines = [f"{HUMAN.capitalize()}'s feedback on the shortlist for {day}:", ""]
                for item_id, mark in unsent.items():
                    comment = f": {mark['comment']}" if mark.get("comment") else ""
                    lines.append(f"- **{mark.get('verdict', '?')}**: {mark.get('title', item_id)} ({item_id}){comment}")
                if SHORTLIST_CONFIG.get("feedback_hint"):
                    lines += ["", SHORTLIST_CONFIG["feedback_hint"]]
                note_name = post_note(Note(to=SHORTLIST_CONFIG.get("feedback_to", "curator"), sender=HUMAN, type="feedback",
                                           subject=f"Feedback on {day} shortlist", body="\n".join(lines))).name
                for mark in unsent.values():
                    mark["sent"] = True
            SHORTLIST.mkdir(parents=True, exist_ok=True)
            feedback_path(day).write_text(json.dumps(feedback, indent=2, ensure_ascii=False), encoding="utf-8")
            return self._json({"ok": True, "feedback": feedback, "note": note_name})

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
