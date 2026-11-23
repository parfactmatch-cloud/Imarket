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
    POST /api/engine        live feed from the running engine (token):
                            {spot, asOf, tradingDate, pdh, pdl, pdc,
                             weekPoc, callWall, putWall, maxPain, pcr,
                             gammaFlip, regime, expiryDate, bars}
                            While a push is fresh (under 10 minutes old)
                            the page shows it as the Fyers engine feed;
                            otherwise it falls back to Yahoo snapshots.
                            Requires the X-Jev-Token header matching the
                            JEV_TERMINAL_TOKEN env var; without that env
                            var the endpoint is disabled.
    POST /api/events        add an engine session-log entry
                            {kind, text}  kind: online|signal|position|squareoff|closed|note
    DELETE /api/events?id=  delete one entry
    POST /api/plan/note     save a note on a plan {planDate, note}
    POST /api/plan/build    rebuild the post-market plan from the latest snapshot

    Every write endpoint above requires the X-Jev-Token header matching
    the JEV_TERMINAL_TOKEN env var (same as POST /api/engine): without
    that env var it answers 503, with a wrong token 401. Read endpoints
    need no token.

State lives in JSON files under the data directory
(``JEV_TERMINAL_DATA_DIR`` or ``terminal/data`` next to this file).
``data/seed.json`` — the last verified session (Mon 16 Nov 2026) —
is used only when no live state exists yet, so the page is never
empty on first boot. Live snapshots overwrite the seed; the seed
itself is never modified.
"""

from __future__ import annotations

import hmac
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
ENGINE_FILE = DATA_DIR / "engine.json"

# Shared secret for the engine feed: the engine sends it as the
# X-Jev-Token header. Set the same value as the JEV_TERMINAL_TOKEN env
# var on the Render service and as the repo secret of the same name.
JEV_TERMINAL_TOKEN = os.environ.get("JEV_TERMINAL_TOKEN", "").strip()

REFRESH_SECONDS = 300  # Yahoo snapshots, at most every 5 minutes
ENGINE_FRESH_SECONDS = 600  # engine push, at most every 10 minutes
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
    """snapshot / plan / chain / events / engine, falling back to the seed."""
    seed = _seed()
    snapshot = _read_json(SNAPSHOT_FILE) or seed.get("snapshot")
    plan = _read_json(PLAN_FILE) or seed.get("plan")
    chain = _read_json(CHAIN_FILE) or seed.get("chainLog")
    events = _read_json(EVENTS_FILE)
    if events is None:
        events = seed.get("events") or []
    engine = _read_json(ENGINE_FILE)
    return {"snapshot": snapshot, "plan": plan, "chain": chain,
            "events": events, "engine": engine}


def _age_seconds(iso_value) -> float | None:
    if not iso_value:
        return None
    try:
        moment = datetime.fromisoformat(iso_value)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _snapshot_age_seconds(snapshot: dict | None) -> float | None:
    if not snapshot:
        return None
    return _age_seconds(snapshot.get("asOf"))


def _engine_is_fresh(engine: dict | None) -> bool:
    """True when the engine pushed a spot within the freshness window."""
    if not engine or engine.get("spot") is None:
        return False
    age = _age_seconds(engine.get("receivedAt"))
    return age is not None and age < ENGINE_FRESH_SECONDS


def refresh_snapshot(force: bool = False) -> dict:
    """Fetch a fresh Yahoo snapshot (unless the cache is still fresh).

    While the engine's Fyers push is fresh, the snapshot is rebuilt
    from it instead — the page then shows live engine data, and the
    Yahoo snapshot is kept only for VIX / Bank Nifty / Sensex.
    Returns the snapshot in use, with the engine chain overlaid.
    Raises RuntimeError if Yahoo fails and there is nothing cached.
    """
    with _lock:
        state = _load_state()
        snapshot = state["snapshot"]
        engine = state["engine"]
        if _engine_is_fresh(engine):
            base = snapshot
            if base is None:
                try:
                    base = jev_terminal.build_snapshot()
                except Exception as exc:
                    raise RuntimeError(
                        "Engine feed is live but no base snapshot and "
                        f"Yahoo failed: {exc}") from exc
                base = jev_terminal.apply_chain_to_snapshot(base, state["chain"])
                _write_json(SNAPSHOT_FILE, base)
            return jev_terminal.apply_engine_snapshot(base, engine)
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

    def _require_token(self) -> bool:
        """Shared-secret check for write endpoints (same as /api/engine).

        Returns True when the request carries the X-Jev-Token header
        matching JEV_TERMINAL_TOKEN; otherwise sends the 503 (token not
        configured) or 401 (wrong token) response and returns False.
        """
        if not JEV_TERMINAL_TOKEN:
            self._send_json(
                {"ok": False,
                 "error": "write endpoints not configured: set JEV_TERMINAL_TOKEN"},
                status=503)
            return False
        presented = self.headers.get("X-Jev-Token") or ""
        if not hmac.compare_digest(presented, JEV_TERMINAL_TOKEN):
            self._send_json({"ok": False, "error": "unauthorized"},
                            status=401)
            return False
        return True

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
            engine = state["engine"]
            return self._send_json({
                "snapshot": snap,
                "chainLog": state["chain"],
                "plan": state["plan"],
                "events": state["events"],
                "engine": ({"receivedAt": engine.get("receivedAt"),
                            "asOf": engine.get("asOf"),
                            "feed": "fyers" if _engine_is_fresh(engine) else "snapshot"}
                           if engine else None),
                "nowIst": datetime.now(jev_terminal.IST).strftime("%H:%M:%S IST"),
            })
        if path in ("/", "/index.html"):
            return super().do_GET()
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        if path == "/api/engine":
            # The engine feed is token-protected: without the shared
            # secret anyone could post fake prices to a public page.
            if not JEV_TERMINAL_TOKEN:
                return self._send_json(
                    {"ok": False,
                     "error": "engine feed not configured: set JEV_TERMINAL_TOKEN"},
                    status=503)
            presented = self.headers.get("X-Jev-Token") or ""
            if not hmac.compare_digest(presented, JEV_TERMINAL_TOKEN):
                return self._send_json({"ok": False, "error": "unauthorized"},
                                       status=401)
            try:
                spot = float(body.get("spot"))
            except (TypeError, ValueError):
                return self._send_json({"ok": False, "error": "spot is required"},
                                       status=400)
            record = {"receivedAt": datetime.now(timezone.utc).isoformat(),
                      "spot": spot}
            for key in ("asOf", "tradingDate", "pdh", "pdl", "pdc", "weekPoc",
                        "callWall", "putWall", "maxPain", "pcr", "gammaFlip",
                        "regime", "expiryDate"):
                if body.get(key) is not None:
                    record[key] = body.get(key)
            if isinstance(body.get("bars"), list):
                record["bars"] = body["bars"]
            _write_json(ENGINE_FILE, record)
            # Keep the logged chain current too, from the same push.
            chain_fields = {k: record.get(k) for k in
                            ("weekPoc", "callWall", "putWall", "pcr", "maxPain")}
            chain_fields = {k: v for k, v in chain_fields.items()
                            if v is not None}
            if chain_fields:
                today = datetime.now(jev_terminal.IST).date().isoformat()
                previous = _read_json(CHAIN_FILE) or {}
                merged = {**previous, **chain_fields, "logDate": today,
                          "createdAt": record["receivedAt"]}
                _write_json(CHAIN_FILE, merged)
            return self._send_json({"ok": True, "engine": record})
        if path == "/api/chain":
            if not self._require_token():
                return
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
            if not self._require_token():
                return
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
            if not self._require_token():
                return
            try:
                return self._send_json(build_plan())
            except RuntimeError as exc:
                return self._send_json({"ok": False, "error": str(exc)}, status=502)
        if path == "/api/plan/note":
            if not self._require_token():
                return
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
            if not self._require_token():
                return
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
