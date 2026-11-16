"""Jev Terminal server — standard library only, no dependencies.

Run:
    python3 server.py            # http://127.0.0.1:8000
    PORT=8000 python3 server.py  # Render / Railway set PORT for you

Why a server at all: Yahoo Finance sends no CORS headers, so a
browser page cannot fetch ^NSEI directly. This server fetches Yahoo
server-side and serves the page + JSON to the browser.

Endpoints:
    GET  /                  the terminal page
    GET  /api/health        {"ok": true, ...}
    GET  /api/snapshot      latest snapshot (refreshes from Yahoo if the
                            cached one is >5 min old and the market is open;
                            ?force=1 refreshes any time)
    GET  /api/terminal      snapshot + chain + plan + events (what the page loads)
    GET  /api/plan          latest post-market plan
    POST /api/chain         log the engine's levels for today:
                            {weekPoc, callWall, putWall, pcr, maxPain}
                            (from the 9:15 run's System Online message and
                            its live chain — nothing here is guessed)
    POST /api/events        add an engine session-log entry
                            {kind, text}  kind: online|signal|position|squareoff|closed|note
    DELETE /api/events?id=  delete one entry
    POST /api/plan/note     save a note on a plan {planDate, note}
    POST /api/plan/build    rebuild the post-market plan from the latest snapshot

State lives in JSON files under the data directory
(``JEV_TERMINAL_DATA_DIR`` or ``terminal/data`` next to this file).
``data/seed.json`` — the last verified session (Mon 16 Nov 2026) —
is used only when no live state exists yet, so the page is never
empty on first boot. Live snapshots overwrite the seed; the seed
itself is never modified.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jev_terminal

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
SEED_FILE = HERE / "data" / "seed.json"

DATA_DIR = Path(os.environ.get("JEV_TERMINAL_DATA_DIR", HERE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
PLAN_FILE = DATA_DIR / "plan.json"
CHAIN_FILE = DATA_DIR / "chain.json"
EVENTS_FILE = DATA_DIR / "events.json"

REFRESH_SECONDS = 300  # Yahoo snapshots, at most every 5 minutes
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Tiny JSON store
# ---------------------------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)


def _seed() -> dict:
    seed = _read_json(SEED_FILE)
    return seed or {}


def _load_state() -> dict:
    """snapshot / plan / chain / events, falling back to the seed."""
    seed = _seed()
    snapshot = _read_json(SNAPSHOT_FILE) or seed.get("snapshot")
    plan = _read_json(PLAN_FILE) or seed.get("plan")
    chain = _read_json(CHAIN_FILE) or seed.get("chainLog")
    events = _read_json(EVENTS_FILE)
    if events is None:
        events = seed.get("events") or []
    return {"snapshot": snapshot, "plan": plan, "chain": chain, "events": events}


def _snapshot_age_seconds(snapshot: dict | None) -> float | None:
    if not snapshot or not snapshot.get("asOf"):
        return None
    try:
        as_of = datetime.fromisoformat(snapshot["asOf"])
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - as_of).total_seconds()


def refresh_snapshot(force: bool = False) -> dict:
    """Fetch a fresh Yahoo snapshot (unless the cache is still fresh).

    Returns the snapshot in use, with the engine chain overlaid.
    Raises RuntimeError if Yahoo fails and there is nothing cached.
    """
    with _lock:
        state = _load_state()
        snapshot = state["snapshot"]
        age = _snapshot_age_seconds(snapshot)
        status = jev_terminal.market_status()
        fresh_enough = age is not None and age < REFRESH_SECONDS
        if not force and fresh_enough:
            return jev_terminal.apply_chain_to_snapshot(snapshot, state["chain"])
        if not force and status != "live" and snapshot:
            # Outside market hours, keep the last snapshot; the plan
            # is what matters then. Still overlay the chain.
            return jev_terminal.apply_chain_to_snapshot(snapshot, state["chain"])
        try:
            fresh = jev_terminal.build_snapshot()
        except Exception as exc:  # Yahoo down / rate-limited / blocked
            if snapshot:
                return jev_terminal.apply_chain_to_snapshot(snapshot, state["chain"])
            raise RuntimeError(f"Yahoo fetch failed and no cached snapshot: {exc}") from exc
        fresh = jev_terminal.apply_chain_to_snapshot(fresh, state["chain"])
        _write_json(SNAPSHOT_FILE, fresh)
        return fresh


def build_plan() -> dict:
    with _lock:
        state = _load_state()
        snapshot = state["snapshot"]
        if not snapshot:
            snapshot = refresh_snapshot(force=True)
        plan = jev_terminal.build_postmarket_plan(snapshot, state["chain"])
        note = ""
        if state["plan"] and state["plan"].get("planDate") == plan["planDate"]:
            note = state["plan"].get("note", "")
        _write_json(PLAN_FILE, {"planDate": plan["planDate"], "note": note, "payload": plan})
        return {"planDate": plan["planDate"], "note": note, "payload": plan}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):  # keep logs quiet
        pass

    def _send_json(self, value, status: int = 200) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/api/health":
            return self._send_json({"ok": True, "service": "jev-terminal",
                                    "marketStatus": jev_terminal.market_status(),
                                    "nowIst": datetime.now(jev_terminal.IST).strftime("%H:%M:%S IST")})
        if path == "/api/snapshot":
            try:
                snap = refresh_snapshot(force=query.get("force") == ["1"])
            except RuntimeError as exc:
                return self._send_json({"ok": False, "error": str(exc)}, status=502)
            return self._send_json(snap)
        if path == "/api/plan":
            state = _load_state()
            return self._send_json(state["plan"] or {})
        if path == "/api/terminal":
            try:
                snap = refresh_snapshot()
            except RuntimeError as exc:
                snap = _load_state()["snapshot"] or {"error": str(exc)}
            state = _load_state()
            return self._send_json({
                "snapshot": snap,
                "chainLog": state["chain"],
                "plan": state["plan"],
                "events": state["events"],
                "nowIst": datetime.now(jev_terminal.IST).strftime("%H:%M:%S IST"),
            })
        if path in ("/", "/index.html"):
            return super().do_GET()
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        if path == "/api/chain":
            chain = {k: body.get(k) for k in
                     ("weekPoc", "callWall", "putWall", "pcr", "maxPain")}
            chain = {k: v for k, v in chain.items() if v is not None}
            if not chain:
                return self._send_json({"ok": False, "error": "no levels supplied"}, status=400)
            today = datetime.now(jev_terminal.IST).date().isoformat()
            record = {"logDate": today, "createdAt": datetime.now(timezone.utc).isoformat(),
                      **chain}
            _write_json(CHAIN_FILE, record)
            # Re-overlay the POC on the cached snapshot right away.
            snap = _read_json(SNAPSHOT_FILE)
            if snap:
                _write_json(SNAPSHOT_FILE,
                            jev_terminal.apply_chain_to_snapshot(snap, record))
            return self._send_json({"ok": True, "chain": record})
        if path == "/api/events":
            kind = str(body.get("kind") or "note")
            text = str(body.get("text") or "").strip()
            if not text:
                return self._send_json({"ok": False, "error": "text is required"}, status=400)
            state = _load_state()
            events = state["events"]
            entry = {"id": (max([e.get("id", 0) for e in events], default=0) + 1),
                     "kind": kind, "text": text,
                     "at": datetime.now(timezone.utc).isoformat()}
            events.append(entry)
            _write_json(EVENTS_FILE, events)
            return self._send_json({"ok": True, "event": entry})
        if path == "/api/plan/build":
            try:
                return self._send_json(build_plan())
            except RuntimeError as exc:
                return self._send_json({"ok": False, "error": str(exc)}, status=502)
        if path == "/api/plan/note":
            plan_date = str(body.get("planDate") or "")
            note = str(body.get("note") or "")
            state = _load_state()
            plan = state["plan"] or {}
            plan["note"] = note
            if plan_date:
                plan["planDate"] = plan_date
            _write_json(PLAN_FILE, plan)
            return self._send_json({"ok": True, "plan": plan})
        return self._send_json({"ok": False, "error": "unknown endpoint"}, status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            try:
                event_id = int(parse_qs(parsed.query).get("id", [""])[0])
            except ValueError:
                return self._send_json({"ok": False, "error": "bad id"}, status=400)
            state = _load_state()
            events = [e for e in state["events"] if e.get("id") != event_id]
            _write_json(EVENTS_FILE, events)
            return self._send_json({"ok": True, "events": events})
        return self._send_json({"ok": False, "error": "unknown endpoint"}, status=404)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Jev Terminal on http://127.0.0.1:{port}  (market: {jev_terminal.market_status()})")
    print("Data dir:", DATA_DIR)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
