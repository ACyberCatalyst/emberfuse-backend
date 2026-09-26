"""
Emberfuse Leaderboard API
==========================
A tiny, dependency-free backend for the Emberfuse leaderboard.

Uses only the Python standard library (http.server + sqlite3), so it
needs nothing installed to run -- just Python 3.9+. That also makes it
trivial to deploy on almost any free hosting tier (Render, Fly.io,
Railway, a Raspberry Pi, whatever).

Endpoints
---------
GET  /health
    -> {"status": "ok"}

GET  /scores/top?limit=20
    -> [{"rank": 1, "player_name": "Ada", "score": 4096, "created_at": "..."}, ...]

GET  /scores/player/<device_id>
    -> {"player_name": "...", "best_score": ..., "rank": ...}  (or 404)

POST /scores
    body: {"player_name": "Ada", "score": 4096, "device_id": "abc-123"}
    -> {"rank": 3, "best_score": 4096, "is_new_best": true}

Run locally
-----------
    python3 app.py
    (defaults to http://127.0.0.1:8000)

Environment variables
----------------------
PORT       - port to listen on (default 8000)
DB_PATH    - path to the sqlite file (default leaderboard.db, next to this file)
"""

import json
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard.db"))
PORT = int(os.environ.get("PORT", "8000"))

MAX_NAME_LEN = 16
MIN_SCORE = 0
MAX_SCORE = 999_999  # sanity cap -- rejects obviously-forged submissions
NAME_ALLOWED = re.compile(r"^[\w \-.'!?]{1,16}$", re.UNICODE)


# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            player_name TEXT    NOT NULL,
            score       INTEGER NOT NULL,
            device_id   TEXT    NOT NULL,
            created_at  INTEGER NOT NULL
        )
        """
    )
    # One row per device: keep only the best score per player, but let
    # everyone submit as many times as they like.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scores_device ON scores(device_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scores_score ON scores(score DESC)"
    )
    conn.commit()
    conn.close()


def upsert_score(player_name: str, score: int, device_id: str):
    """Insert this device's score, or update it if it's a new personal best."""
    conn = get_db()
    row = conn.execute(
        "SELECT score FROM scores WHERE device_id = ?", (device_id,)
    ).fetchone()

    is_new_best = True
    if row is not None:
        if score > row["score"]:
            conn.execute(
                "UPDATE scores SET player_name = ?, score = ?, created_at = ? WHERE device_id = ?",
                (player_name, score, int(time.time()), device_id),
            )
        else:
            is_new_best = False
            # Still let them update their display name even if the score isn't a new best.
            conn.execute(
                "UPDATE scores SET player_name = ? WHERE device_id = ?",
                (player_name, device_id),
            )
    else:
        conn.execute(
            "INSERT INTO scores (player_name, score, device_id, created_at) VALUES (?, ?, ?, ?)",
            (player_name, score, device_id, int(time.time())),
        )

    conn.commit()

    best_score = conn.execute(
        "SELECT score FROM scores WHERE device_id = ?", (device_id,)
    ).fetchone()["score"]

    rank = conn.execute(
        "SELECT COUNT(*) + 1 AS rank FROM scores WHERE score > ?", (best_score,)
    ).fetchone()["rank"]

    conn.close()
    return {"rank": rank, "best_score": best_score, "is_new_best": is_new_best}


def get_top_scores(limit: int, requesting_device_id: str = ""):
    conn = get_db()
    rows = conn.execute(
        "SELECT player_name, score, created_at, device_id FROM scores ORDER BY score DESC, created_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    # Never expose raw device_id values to clients -- only whether a row
    # belongs to the caller, so "(You)" can be highlighted client-side.
    return [
        {
            "rank": i + 1,
            "player_name": row["player_name"],
            "score": row["score"],
            "created_at": row["created_at"],
            "is_me": bool(requesting_device_id) and row["device_id"] == requesting_device_id,
        }
        for i, row in enumerate(rows)
    ]


def get_player(device_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT player_name, score FROM scores WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return None

    rank = conn.execute(
        "SELECT COUNT(*) + 1 AS rank FROM scores WHERE score > ?", (row["score"],)
    ).fetchone()["rank"]
    conn.close()
    return {"player_name": row["player_name"], "best_score": row["score"], "rank": rank}


# ------------------------------------------------------------------
# VALIDATION
# ------------------------------------------------------------------
class ValidationError(Exception):
    pass


def validate_submission(payload: dict):
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object.")

    name = str(payload.get("player_name", "")).strip()
    if not name:
        raise ValidationError("player_name is required.")
    if len(name) > MAX_NAME_LEN or not NAME_ALLOWED.match(name):
        raise ValidationError(f"player_name must be 1-{MAX_NAME_LEN} characters (letters, numbers, spaces, basic punctuation).")

    score = payload.get("score")
    if not isinstance(score, int) or isinstance(score, bool):
        raise ValidationError("score must be an integer.")
    if score < MIN_SCORE or score > MAX_SCORE:
        raise ValidationError(f"score must be between {MIN_SCORE} and {MAX_SCORE}.")

    device_id = str(payload.get("device_id", "")).strip()
    if not device_id or len(device_id) > 64:
        raise ValidationError("device_id is required (max 64 characters).")

    return name, score, device_id


# ------------------------------------------------------------------
# HTTP HANDLER
# ------------------------------------------------------------------
class LeaderboardHandler(BaseHTTPRequestHandler):
    server_version = "EmberfuseLeaderboard/1.0"

    def _send_json(self, status: int, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Permissive CORS: Godot's HTTPRequest doesn't need it, but it's
        # handy if you ever hit this API from a browser-based tool too.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Quieter default logging; comment this out if you want full access logs.
        print("[leaderboard] " + (fmt % args))

    def do_OPTIONS(self):
        self._send_json(200, {"status": "ok"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if path == "/scores/top":
            try:
                limit = int(query.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(limit, 100))
            requesting_device_id = query.get("device_id", [""])[0]
            self._send_json(200, get_top_scores(limit, requesting_device_id))
            return

        if path.startswith("/scores/player/"):
            device_id = path[len("/scores/player/"):]
            player = get_player(device_id)
            if player is None:
                self._send_json(404, {"error": "No scores for this device yet."})
            else:
                self._send_json(200, player)
            return

        self._send_json(404, {"error": "Not found."})

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path != "/scores":
            self._send_json(404, {"error": "Not found."})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Body must be valid JSON."})
            return

        try:
            name, score, device_id = validate_submission(payload)
        except ValidationError as e:
            self._send_json(400, {"error": str(e)})
            return

        result = upsert_score(name, score, device_id)
        self._send_json(201, result)


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), LeaderboardHandler)
    print(f"Emberfuse leaderboard API running on http://0.0.0.0:{PORT}")
    print(f"Database file: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
