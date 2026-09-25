"""
Acceso a datos.

* Si existe la variable TURSO_DATABASE_URL se usa Turso (protocolo HTTP "Hrana",
  solo requiere `httpx`, no compila nada, funciona en cualquier hosting).
* Si NO existe, se usa un archivo SQLite local (LOCAL_DB_PATH) para que puedas
  probar el juego en tu computador sin configurar nada.

Interfaz común:
    db.execute(sql, args)  -> list[dict]
    db.batch([(sql, args), ...])  -> ejecuta todo en UNA transacción (todo o nada)
"""
from __future__ import annotations

import base64
import os
import sqlite3
import threading
from typing import Any, Iterable, Sequence

Statement = tuple[str, Sequence[Any]]


class DBError(Exception):
    pass


# --------------------------------------------------------------------------
# Esquema persistente: partidas, jugadores, movimientos, cuentas y chat.
# --------------------------------------------------------------------------
SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS games (
        code          TEXT PRIMARY KEY,
        mode          TEXT NOT NULL,              -- 'local' | 'online'
        status        TEXT NOT NULL,              -- 'lobby' | 'playing' | 'finished'
        pot           INTEGER NOT NULL DEFAULT 0,
        ante          INTEGER NOT NULL,
        initial_chips INTEGER NOT NULL,
        current_seat  INTEGER NOT NULL DEFAULT 0,
        phase         TEXT NOT NULL DEFAULT 'first_roll',  -- 'first_roll' | 'bet' | 'done'
        first_roll    INTEGER,
        winner_seat   INTEGER,
        finish_reason TEXT,
        turn_no       INTEGER NOT NULL DEFAULT 0,
        turn_deadline REAL,
        version       INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS players (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        game_code TEXT NOT NULL REFERENCES games(code) ON DELETE CASCADE,
        seat      INTEGER NOT NULL,
        name      TEXT NOT NULL,
        token     TEXT NOT NULL,
        chips     INTEGER NOT NULL,
        is_host   INTEGER NOT NULL DEFAULT 0,
        joined_at TEXT NOT NULL,
        UNIQUE (game_code, seat)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS moves (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        game_code   TEXT NOT NULL REFERENCES games(code) ON DELETE CASCADE,
        turn_no     INTEGER NOT NULL,
        seat        INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        kind        TEXT NOT NULL,   -- 'pone' | 'saca' | 'pasa' | 'gana' | 'pierde'
        first_roll  INTEGER,
        second_roll INTEGER,
        bet         INTEGER NOT NULL DEFAULT 0,
        pot_after   INTEGER NOT NULL,
        chips_after INTEGER NOT NULL,
        message     TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        display_name  TEXT NOT NULL,
        avatar        TEXT NOT NULL DEFAULT '🧑',
        badge         TEXT NOT NULL DEFAULT '🎲',
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        game_code  TEXT NOT NULL REFERENCES games(code) ON DELETE CASCADE,
        player_name TEXT NOT NULL,
        message    TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_players_game ON players(game_code)",
    "CREATE INDEX IF NOT EXISTS idx_moves_game ON moves(game_code, id)",
    "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_game ON chat_messages(game_code, id)",
]


# --------------------------------------------------------------------------
# Backend Turso (HTTP): producción y datos compartidos entre dispositivos.
# --------------------------------------------------------------------------
def _encode_arg(v: Any) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _decode_cell(cell: dict) -> Any:
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        return base64.b64decode(cell["base64"] + "==")
    return cell.get("value")


def _decode_result(result: dict) -> list[dict]:
    cols = [c.get("name") for c in result.get("cols", [])]
    return [
        {cols[i]: _decode_cell(cell) for i, cell in enumerate(row)}
        for row in result.get("rows", [])
    ]


class _TursoBackend:
    def __init__(self, url: str, token: str | None) -> None:
        import httpx  # import perezoso: solo se necesita con Turso

        self._httpx = httpx
        self.base = url.replace("libsql://", "https://").rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(timeout=20)

    def _pipeline(self, requests: list[dict]) -> list[dict]:
        try:
            resp = self.client.post(
                f"{self.base}/v2/pipeline",
                headers=self.headers,
                json={"requests": requests + [{"type": "close"}]},
            )
        except self._httpx.HTTPError as exc:  # red caída, DNS, timeout...
            raise DBError(f"No se pudo conectar con Turso: {exc}") from exc
        if resp.status_code == 401:
            raise DBError("Turso rechazó el token (401). Revisa TURSO_AUTH_TOKEN.")
        if resp.status_code >= 400:
            raise DBError(f"Turso respondió {resp.status_code}: {resp.text[:300]}")
        results = resp.json()["results"]
        for r in results:
            if r.get("type") == "error":
                raise DBError(r["error"].get("message", "Error en Turso"))
        return results

    def execute(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        stmt = {"sql": sql, "args": [_encode_arg(a) for a in args]}
        results = self._pipeline([{"type": "execute", "stmt": stmt}])
        return _decode_result(results[0]["response"]["result"])

    def batch(self, statements: Iterable[Statement]) -> None:
        stmts = list(statements)
        if not stmts:
            return
        n = len(stmts)
        steps: list[dict] = [{"stmt": {"sql": "BEGIN"}}]
        for i, (sql, args) in enumerate(stmts):
            steps.append(
                {
                    "stmt": {"sql": sql, "args": [_encode_arg(a) for a in args]},
                    "condition": {"type": "ok", "step": i},  # solo si el paso previo salió bien
                }
            )
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": n}})
        steps.append(
            {
                "stmt": {"sql": "ROLLBACK"},
                "condition": {"type": "not", "cond": {"type": "ok", "step": n + 1}},
            }
        )
        results = self._pipeline([{"type": "batch", "batch": {"steps": steps}}])
        outcome = results[0]["response"]["result"]
        errors = [e for e in outcome.get("step_errors", []) if e]
        if errors:
            raise DBError(errors[0].get("message", "Error en la transacción"))


# --------------------------------------------------------------------------
# Backend SQLite local: respaldo para desarrollo sin credenciales de Turso.
# --------------------------------------------------------------------------
class _SqliteBackend:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.Lock()

    def execute(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        with self.lock:
            try:
                cur = self.conn.execute(sql, tuple(args))
                return [dict(r) for r in cur.fetchall()]
            except sqlite3.Error as exc:
                raise DBError(str(exc)) from exc

    def batch(self, statements: Iterable[Statement]) -> None:
        with self.lock:
            try:
                self.conn.execute("BEGIN")
                for sql, args in statements:
                    self.conn.execute(sql, tuple(args))
                self.conn.execute("COMMIT")
            except sqlite3.Error as exc:
                self.conn.execute("ROLLBACK")
                raise DBError(str(exc)) from exc


# --------------------------------------------------------------------------
# Fachada
# --------------------------------------------------------------------------
class Database:
    def __init__(self) -> None:
        url = os.getenv("TURSO_DATABASE_URL", "").strip()
        if url:
            self.kind = "turso"
            self._b: _TursoBackend | _SqliteBackend = _TursoBackend(
                url, os.getenv("TURSO_AUTH_TOKEN", "").strip() or None
            )
        else:
            self.kind = "sqlite"
            self._b = _SqliteBackend(os.getenv("LOCAL_DB_PATH", "guayabita_local.db"))

    def execute(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        return self._b.execute(sql, args)

    def batch(self, statements: Iterable[Statement]) -> None:
        self._b.batch(statements)

    def init_schema(self) -> None:
        for stmt in SCHEMA:
            self._b.execute(stmt, ())
        try:
            self._b.execute("ALTER TABLE games ADD COLUMN turn_deadline REAL", ())
        except DBError as exc:
            if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower():
                raise
