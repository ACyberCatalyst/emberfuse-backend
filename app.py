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
GET  /
    -> EmberFuse developer website

GET  /privacy
    -> EmberFuse privacy policy

GET  /app-ads.txt
    -> AdMob authorized seller declaration

GET  /health
    -> {"status": "ok"}

GET  /scores/top?limit=100&offset=0
    -> [{"rank": 1, "player_name": "Ada", "score": 4096, "created_at": "..."}, ...]

GET  /scores/player/<device_id>
    -> {"player_name": "...", "best_score": ..., "rank": ...}  (or 404)

POST /players/name
    body: {"player_name": "Ada", "device_id": "abc-123"}
    -> saves or renames that player's public display name

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
import sqlite3
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "leaderboard.db"))
PORT = int(os.environ.get("PORT", "8000"))
PUBLIC_FILES_DIR = Path(__file__).resolve().parent

MAX_NAME_LEN = 40
MIN_SCORE = 0
MAX_SCORE = 9_007_199_254_740_991  # largest integer that round-trips through JSON numbers
NAME_PUNCTUATION = set(" _-.'!?’")


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

    # Every player needs a distinct public name. Keep the normalized key
    # unique, while allowing full names and Unicode letters in the name itself.
    players_schema = """
        CREATE TABLE IF NOT EXISTS players (
            device_id   TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            name_key    TEXT NOT NULL UNIQUE,
            created_at  INTEGER NOT NULL
        )
    """
    conn.execute(players_schema)
    has_unique_name_key = False
    for index in conn.execute("PRAGMA index_list(players)").fetchall():
        if not index["unique"]:
            continue
        escaped_index_name = index["name"].replace('"', '""')
        index_columns = conn.execute(
            f'PRAGMA index_info("{escaped_index_name}")'
        ).fetchall()
        if [column["name"] for column in index_columns] == ["name_key"]:
            has_unique_name_key = True
            break

    if not has_unique_name_key:
        old_players = conn.execute(
            "SELECT device_id, player_name, created_at FROM players ORDER BY created_at ASC, device_id ASC"
        ).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE players")
        conn.execute(players_schema)
        used_names = set()
        for player in old_players:
            base_name = player["player_name"].strip()[:MAX_NAME_LEN] or "Player"
            name = base_name
            suffix = 2
            while name.casefold() in used_names:
                tail = " (%d)" % suffix
                name = base_name[:MAX_NAME_LEN - len(tail)].rstrip() + tail
                suffix += 1
            used_names.add(name.casefold())
            conn.execute(
                "INSERT INTO players (device_id, player_name, name_key, created_at) VALUES (?, ?, ?, ?)",
                (player["device_id"], name, name.casefold(), player["created_at"]),
            )
            conn.execute(
                "UPDATE scores SET player_name = ? WHERE device_id = ?",
                (name, player["device_id"]),
            )
        conn.commit()

    # Migrate existing leaderboard rows into the player registry.
    scores = conn.execute(
        "SELECT device_id, player_name, created_at FROM scores ORDER BY created_at ASC, id ASC"
    ).fetchall()
    used_names = {
        row["name_key"]
        for row in conn.execute("SELECT name_key FROM players").fetchall()
    }
    for score_row in scores:
        device_id = score_row["device_id"]
        registered = conn.execute(
            "SELECT player_name, name_key FROM players WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if registered is not None:
            conn.execute(
                "UPDATE scores SET player_name = ? WHERE device_id = ?",
                (registered["player_name"], device_id),
            )
            continue

        base_name = score_row["player_name"].strip()[:MAX_NAME_LEN] or "Player"
        name = base_name
        suffix = 2
        while name.casefold() in used_names:
            tail = " (%d)" % suffix
            name = base_name[:MAX_NAME_LEN - len(tail)].rstrip() + tail
            suffix += 1
        name_key = name.casefold()
        conn.execute(
            "INSERT INTO players (device_id, player_name, name_key, created_at) VALUES (?, ?, ?, ?)",
            (device_id, name, name_key, score_row["created_at"]),
        )
        conn.execute(
            "UPDATE scores SET player_name = ? WHERE device_id = ?",
            (name, device_id),
        )
        used_names.add(name_key)

    conn.commit()
    conn.close()


def upsert_score(player_name: str, score: int, device_id: str):
    """Insert this device's score, or update it if it's a new personal best."""
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    player = conn.execute(
        "SELECT player_name FROM players WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    if player is None:
        owner = conn.execute(
            "SELECT device_id FROM players WHERE name_key = ?",
            (player_name.casefold(),),
        ).fetchone()
        if owner is not None:
            conn.rollback()
            conn.close()
            raise NameTakenError("That name is already in use. Please choose another name.")
        conn.execute(
            "INSERT INTO players (device_id, player_name, name_key, created_at) VALUES (?, ?, ?, ?)",
            (device_id, player_name, player_name.casefold(), int(time.time())),
        )
    elif player["player_name"].casefold() != player_name.casefold():
        conn.rollback()
        conn.close()
        raise NameMismatchError("This device has a different saved name. Change it in Settings first.")

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


def reserve_player_name(player_name: str, device_id: str):
    """Create or rename the player's unique public leaderboard name."""
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    name_key = player_name.casefold()
    owner = conn.execute(
        "SELECT device_id FROM players WHERE name_key = ?", (name_key,)
    ).fetchone()
    if owner is not None and owner["device_id"] != device_id:
        conn.rollback()
        conn.close()
        raise NameTakenError("That name is already in use. Please choose another name.")

    existing = conn.execute(
        "SELECT created_at FROM players WHERE device_id = ?", (device_id,)
    ).fetchone()
    created_at = existing["created_at"] if existing is not None else int(time.time())
    conn.execute(
        "INSERT INTO players (device_id, player_name, name_key, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(device_id) DO UPDATE SET player_name = excluded.player_name, name_key = excluded.name_key",
        (device_id, player_name, name_key, created_at),
    )
    conn.execute(
        "UPDATE scores SET player_name = ? WHERE device_id = ?",
        (player_name, device_id),
    )
    conn.commit()
    conn.close()
    return {"player_name": player_name, "ok": True}


def get_top_scores(limit: int, offset: int = 0, requesting_device_id: str = ""):
    conn = get_db()
    rows = conn.execute(
        "SELECT player_name, score, created_at, device_id FROM scores ORDER BY score DESC, created_at ASC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    conn.close()

    # Never expose raw device_id values to clients -- only whether a row
    # belongs to the caller, so "(You)" can be highlighted client-side.
    return [
        {
            "rank": offset + i + 1,
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


class NameTakenError(Exception):
    pass


class NameMismatchError(Exception):
    pass


def validate_submission(payload: dict):
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object.")

    name = validate_player_name(payload.get("player_name", ""))

    score = payload.get("score")
    if not isinstance(score, int) or isinstance(score, bool):
        raise ValidationError("score must be an integer.")
    if score < MIN_SCORE or score > MAX_SCORE:
        raise ValidationError(f"score must be between {MIN_SCORE} and {MAX_SCORE}.")

    device_id = validate_device_id(payload.get("device_id", ""))

    return name, score, device_id


def validate_player_name(value):
    name = unicodedata.normalize("NFC", str(value).strip())
    if not name:
        raise ValidationError("player_name is required.")
    has_only_allowed_characters = all(
        unicodedata.category(char)[0] in "LMN" or char in NAME_PUNCTUATION
        for char in name
    )
    if len(name) > MAX_NAME_LEN or not has_only_allowed_characters:
        raise ValidationError(f"player_name must be 1-{MAX_NAME_LEN} characters (letters, numbers, spaces, and common name punctuation).")
    return name


def validate_device_id(value):
    device_id = str(value).strip()
    if not device_id or len(device_id) > 64:
        raise ValidationError("device_id is required (max 64 characters).")
    return device_id


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

    def _send_public_file(self, file_name: str, content_type: str):
        file_path = PUBLIC_FILES_DIR / file_name
        try:
            body = file_path.read_bytes()
        except OSError:
            body = b"Not found.\n"
            status = 404
            content_type = "text/plain; charset=utf-8"
        else:
            status = 200

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300")
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

        if path == "/":
            self._send_public_file("privacy.html", "text/html; charset=utf-8")
            return

        if path in ("/privacy", "/privacy/"):
            self._send_public_file("privacy.html", "text/html; charset=utf-8")
            return

        if path == "/app-ads.txt":
            self._send_public_file("app-ads.txt", "text/plain; charset=utf-8")
            return

        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if path == "/scores/top":
            try:
                limit = int(query.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            limit = max(1, min(limit, 1000))
            try:
                offset = max(0, int(query.get("offset", ["0"])[0]))
            except ValueError:
                offset = 0
            requesting_device_id = query.get("device_id", [""])[0]
            self._send_json(200, get_top_scores(limit, offset, requesting_device_id))
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

        if parsed.path not in ("/scores", "/players/name"):
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
            if parsed.path == "/players/name":
                if not isinstance(payload, dict):
                    raise ValidationError("Body must be a JSON object.")
                name = validate_player_name(payload.get("player_name", ""))
                device_id = validate_device_id(payload.get("device_id", ""))
                result = reserve_player_name(name, device_id)
                self._send_json(200, result)
                return

            name, score, device_id = validate_submission(payload)
            result = upsert_score(name, score, device_id)
            self._send_json(201, result)
        except ValidationError as e:
            self._send_json(400, {"error": str(e)})
        except NameTakenError as e:
            self._send_json(409, {"error": str(e)})
        except NameMismatchError as e:
            self._send_json(409, {"error": str(e)})


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

