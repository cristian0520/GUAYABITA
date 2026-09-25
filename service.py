"""
Lógica de partidas: conecta las reglas (engine) con la base de datos (db).
No depende de FastAPI, así que se puede probar por separado.
"""
from __future__ import annotations

import secrets
import time
import base64
import hashlib
import hmac
import re
from datetime import timedelta
from datetime import datetime, timezone
from typing import Any

import engine
from db import Database, DBError

MAX_PLAYERS = 8
MIN_PLAYERS = 2
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O/1/I para evitar confusiones
TURN_SECONDS = 15  # sin 0/O/1/I para evitar confusiones
HISTORY_LIMIT = 40


class GameError(Exception):
    """Error de negocio con código HTTP sugerido."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GameService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Usuarios
    # ------------------------------------------------------------------
    @staticmethod
    def _username(value: str) -> str:
        username = (value or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,20}", username):
            raise GameError("El usuario debe tener 3-20 caracteres: letras, números o guion bajo.")
        return username

    @staticmethod
    def _password_hash(password: str, salt: bytes | None = None) -> str:
        if len(password or "") < 8:
            raise GameError("La contraseña debe tener al menos 8 caracteres.")
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return f"scrypt${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"

    @staticmethod
    def _check_password(password: str, encoded: str) -> bool:
        try:
            _, salt_text, digest_text = encoded.split("$")
            salt = base64.urlsafe_b64decode(salt_text.encode())
            expected = base64.urlsafe_b64decode(digest_text.encode())
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _public_user(row: dict) -> dict:
        return {
            "id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "avatar": row["avatar"], "badge": row["badge"],
        }

    def _auth_user(self, token: str | None) -> dict:
        if not token:
            raise GameError("Inicia sesión para continuar.", 401)
        rows = self.db.execute(
            "SELECT u.* FROM users u JOIN auth_sessions s ON s.user_id=u.id "
            "WHERE s.token=? AND s.expires_at>?",
            [token, _now()],
        )
        if not rows:
            raise GameError("La sesión expiró. Inicia sesión de nuevo.", 401)
        return rows[0]

    def register_user(self, username: str, password: str, display_name: str = "") -> dict:
        username = self._username(username)
        display_name = " ".join((display_name or username).split())[:20] or username
        password_hash = self._password_hash(password)
        try:
            self.db.batch([(
                "INSERT INTO users (username,password_hash,display_name,created_at) VALUES (?,?,?,?)",
                [username, password_hash, display_name, _now()],
            )])
        except DBError as exc:
            if "UNIQUE" in str(exc).upper():
                raise GameError("Ese nombre de usuario ya está registrado.", 409) from exc
            raise
        return self.login_user(username, password)

    def login_user(self, username: str, password: str) -> dict:
        username = self._username(username)
        rows = self.db.execute("SELECT * FROM users WHERE username=?", [username])
        if not rows or not self._check_password(password, rows[0]["password_hash"]):
            raise GameError("Usuario o contraseña incorrectos.", 401)
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=30)
        self.db.execute(
            "INSERT INTO auth_sessions (token,user_id,expires_at,created_at) VALUES (?,?,?,?)",
            [token, rows[0]["id"], expires.isoformat(timespec="seconds"), _now()],
        )
        return {"token": token, "user": self._public_user(rows[0])}

    def current_user(self, token: str | None) -> dict:
        return self._public_user(self._auth_user(token))

    def update_profile(self, token: str | None, avatar: str, display_name: str = "") -> dict:
        user = self._auth_user(token)
        allowed = {"🧑", "👩", "🧔", "👨", "👩‍🦱", "🧑‍🎤", "👨‍🦰", "👩‍🦳", "🐯", "🦊", "🐼", "🐸"}
        if avatar not in allowed:
            raise GameError("Ese avatar no está disponible.")
        name = " ".join((display_name or user["display_name"]).split())[:20]
        if not name:
            raise GameError("El nombre visible no puede estar vacío.")
        self.db.execute("UPDATE users SET avatar=?, display_name=? WHERE id=?", [avatar, name, user["id"]])
        return {"user": self._public_user(self._auth_user(token))}

    def logout_user(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM auth_sessions WHERE token=?", [token])

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    def _game(self, code: str) -> dict:
        rows = self.db.execute("SELECT * FROM games WHERE code = ?", [code.upper()])
        if not rows:
            raise GameError("No existe una partida con ese código.", 404)
        return rows[0]

    def _players(self, code: str) -> list[dict]:
        return self.db.execute(
            "SELECT seat, name, token, chips, is_host FROM players "
            "WHERE game_code = ? ORDER BY seat",
            [code],
        )

    def _moves(self, code: str) -> list[dict]:
        return self.db.execute(
            "SELECT turn_no, seat, player_name, kind, first_roll, second_roll, bet, "
            "pot_after, message FROM moves WHERE game_code = ? "
            "ORDER BY id DESC LIMIT ?",
            [code, HISTORY_LIMIT],
        )

    def _chat(self, code: str) -> list[dict]:
        return self.db.execute(
            "SELECT player_name, message, created_at FROM chat_messages "
            "WHERE game_code=? ORDER BY id DESC LIMIT 40", [code]
        )

    def list_lobbies(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT g.code, g.ante, g.initial_chips, g.created_at, COUNT(p.seat) AS players "
            "FROM games g LEFT JOIN players p ON p.game_code=g.code "
            "WHERE g.mode='online' AND g.status='lobby' "
            "GROUP BY g.code ORDER BY g.created_at DESC LIMIT 100"
        )
        return rows

    def _new_code(self) -> str:
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
            if not self.db.execute("SELECT 1 AS x FROM games WHERE code = ?", [code]):
                return code
        raise GameError("No se pudo generar un código, intenta de nuevo.", 500)

    # ------------------------------------------------------------------
    # Estado público (nunca expone tokens de otros jugadores)
    # ------------------------------------------------------------------
    def state(self, code: str, token: str | None = None) -> dict:
        game = self._game(code)
        if self._expire_turn_if_needed(game):
            game = self._game(code)
        players = self._players(game["code"])
        me = next((p for p in players if token and p["token"] == token), None)
        playing = game["status"] == "playing"
        current = next((p for p in players if p["seat"] == game["current_seat"]), None)

        can_act = False
        if playing and current:
            if game["mode"] == "local":
                can_act = True
            else:
                can_act = bool(me and me["seat"] == current["seat"])

        limit = engine.max_bet(game["pot"], current["chips"]) if current and playing else 0

        return {
            "code": game["code"],
            "mode": game["mode"],
            "status": game["status"],
            "pot": game["pot"],
            "ante": game["ante"],
            "initial_chips": game["initial_chips"],
            "phase": game["phase"],
            "first_roll": game["first_roll"],
            "current_seat": game["current_seat"] if playing else None,
            "winner_seat": game["winner_seat"],
            "finish_reason": game["finish_reason"],
            "turn_no": game["turn_no"],
            "turn_deadline": game["turn_deadline"],
            "version": game["version"],
            "players": [
                {
                    "seat": p["seat"],
                    "name": p["name"],
                    "chips": p["chips"],
                    "is_host": bool(p["is_host"]),
                    "out": game["status"] != "lobby" and p["chips"] <= 0,
                }
                for p in players
            ],
            "you": (
                {"seat": me["seat"], "name": me["name"], "is_host": bool(me["is_host"])}
                if me
                else None
            ),
            "can_act": can_act,
            "max_bet": limit,
            "moves": self._moves(game["code"]),
            "chat": self._chat(game["code"]),
        }

    def send_chat(self, code: str, token: str | None, message: str) -> dict:
        game = self._game(code)
        player = next((p for p in self._players(game["code"]) if token and p["token"] == token), None)
        if not player:
            raise GameError("No tienes permiso para escribir en esta sala.", 403)
        clean = " ".join((message or "").split())
        if not clean:
            raise GameError("Escribe un mensaje.")
        if len(clean) > 180:
            raise GameError("El mensaje no puede superar 180 caracteres.")
        self.db.execute(
            "INSERT INTO chat_messages (game_code,player_name,message,created_at) VALUES (?,?,?,?)",
            [game["code"], player["name"], clean, _now()],
        )
        return self.state(game["code"], token)

    def change_seat(self, code: str, token: str | None, seat: int) -> dict:
        game = self._game(code)
        if game["status"] != "lobby":
            raise GameError("Solo puedes cambiar de silla antes de iniciar la partida.", 409)
        players = self._players(game["code"])
        player = next((p for p in players if token and p["token"] == token), None)
        if not player:
            raise GameError("No tienes permiso para cambiar de silla.", 403)
        if seat < 0 or seat >= MAX_PLAYERS:
            raise GameError("Ese asiento no existe.")
        if any(p["seat"] == seat for p in players):
            raise GameError("Ese asiento ya está ocupado.")
        self.db.execute("UPDATE players SET seat=? WHERE game_code=? AND seat=?", [seat, game["code"], player["seat"]])
        return self.state(game["code"], token)

    # ------------------------------------------------------------------
    # Crear / unirse / iniciar
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_name(name: str) -> str:
        name = " ".join((name or "").split())[:20]
        if not name:
            raise GameError("Escribe un nombre.")
        return name

    @staticmethod
    def _check_stakes(ante: int, initial_chips: int) -> None:
        if ante < 1:
            raise GameError("La apuesta inicial debe ser de al menos 1 ficha.")
        if initial_chips <= ante:
            raise GameError("Las fichas iniciales deben ser más que la apuesta inicial.")

    def create_game(
        self,
        mode: str,
        ante: int,
        initial_chips: int,
        host_name: str = "",
        names: list[str] | None = None,
    ) -> dict:
        if mode not in ("local", "online"):
            raise GameError("Modo inválido.")
        self._check_stakes(ante, initial_chips)

        code = self._new_code()
        now = _now()
        chips = initial_chips - ante
        stmts: list[tuple[str, list[Any]]] = []
        host_token = secrets.token_urlsafe(16)

        if mode == "local":
            clean = [self._clean_name(n) for n in (names or []) if (n or "").strip()]
            if not (MIN_PLAYERS <= len(clean) <= MAX_PLAYERS):
                raise GameError(f"Se necesitan entre {MIN_PLAYERS} y {MAX_PLAYERS} jugadores.")
            if len({n.lower() for n in clean}) != len(clean):
                raise GameError("Los nombres de los jugadores deben ser distintos.")
            stmts.append(
                (
                    "INSERT INTO games (code, mode, status, pot, ante, initial_chips, "
                    "current_seat, phase, created_at, updated_at) "
                    "VALUES (?, 'local', 'playing', ?, ?, ?, 0, 'first_roll', ?, ?)",
                    [code, ante * len(clean), ante, initial_chips, now, now],
                )
            )
            for seat, n in enumerate(clean):
                stmts.append(
                    (
                        "INSERT INTO players (game_code, seat, name, token, chips, is_host, joined_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [code, seat, n, host_token if seat == 0 else secrets.token_urlsafe(16),
                         chips, 1 if seat == 0 else 0, now],
                    )
                )
        else:
            host = self._clean_name(host_name)
            stmts.append(
                (
                    "INSERT INTO games (code, mode, status, pot, ante, initial_chips, "
                    "current_seat, phase, created_at, updated_at) "
                    "VALUES (?, 'online', 'lobby', 0, ?, ?, 0, 'first_roll', ?, ?)",
                    [code, ante, initial_chips, now, now],
                )
            )
            stmts.append(
                (
                    "INSERT INTO players (game_code, seat, name, token, chips, is_host, joined_at) "
                    "VALUES (?, 0, ?, ?, ?, 1, ?)",
                    [code, host, host_token, chips, now],
                )
            )

        self._write(stmts)
        return {"code": code, "token": host_token}

    def join(self, code: str, name: str) -> dict:
        game = self._game(code)
        if game["mode"] != "online":
            raise GameError("Esta partida es de un solo dispositivo; no admite unirse.")
        if game["status"] != "lobby":
            raise GameError("La partida ya empezó.", 409)
        name = self._clean_name(name)
        players = self._players(game["code"])
        if len(players) >= MAX_PLAYERS:
            raise GameError("La mesa está llena.", 409)
        if any(p["name"].lower() == name.lower() for p in players):
            raise GameError("Ya hay un jugador con ese nombre.", 409)

        token = secrets.token_urlsafe(16)
        seat = max((p["seat"] for p in players), default=-1) + 1
        self._write(
            [
                (
                    "INSERT INTO players (game_code, seat, name, token, chips, is_host, joined_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    [game["code"], seat, name, token, game["initial_chips"] - game["ante"], _now()],
                )
            ],
            conflict_msg="Otro jugador entró al mismo tiempo. Intenta de nuevo.",
        )
        return {"code": game["code"], "token": token}

    def start(self, code: str, token: str | None) -> dict:
        game = self._game(code)
        players = self._players(game["code"])
        me = next((p for p in players if token and p["token"] == token), None)
        if not me or not me["is_host"]:
            raise GameError("Solo quien creó la partida puede iniciarla.", 403)
        if game["status"] != "lobby":
            raise GameError("La partida ya empezó.", 409)
        if len(players) < MIN_PLAYERS:
            raise GameError(f"Se necesitan al menos {MIN_PLAYERS} jugadores.")
        self._write(
            [
                (
                    "UPDATE games SET status='playing', pot=?, current_seat=?, phase='first_roll', "
                    "turn_deadline=?, version=version+1, updated_at=? WHERE code=?",
                    [game["ante"] * len(players), players[0]["seat"], time.time() + TURN_SECONDS,
                     _now(), game["code"]],
                )
            ]
        )
        return self.state(game["code"], token)

    def recharge(self, code: str, token: str | None, seat: int | None, amount: int) -> dict:
        """Recarga fichas de un jugador sin reiniciar la mesa."""
        if amount < 1:
            raise GameError("La recarga debe ser mayor que cero.")
        game = self._game(code)
        players = self._players(game["code"])
        requester = next((p for p in players if token and p["token"] == token), None)
        if not requester:
            raise GameError("No tienes permiso para recargar en esta mesa.", 403)

        target_seat = requester["seat"] if game["mode"] == "online" else seat
        if target_seat is None:
            raise GameError("Selecciona el jugador que va a recargar.")
        player = next((p for p in players if p["seat"] == target_seat), None)
        if not player:
            raise GameError("Ese jugador no existe en la mesa.", 404)
        if game["mode"] == "online" and player["seat"] != requester["seat"]:
            raise GameError("Solo puedes recargar tu propio saldo.", 403)
        if player["chips"] > 0:
            raise GameError("Solo puedes recargar cuando te quedas sin saldo.")

        self._write(
            [
                (
                    "UPDATE players SET chips=? WHERE game_code=? AND seat=?",
                    [amount, game["code"], player["seat"]],
                ),
                (
                    "UPDATE games SET status='playing', phase='first_roll', "
                    "current_seat=?, winner_seat=NULL, finish_reason=NULL, turn_deadline=?, "
                    "version=version+1, updated_at=? WHERE code=? AND status='finished'",
                    [target_seat, time.time() + TURN_SECONDS, _now(), game["code"]],
                ),
            ]
        )
        return self.state(game["code"], token)

    def restart_round(self, code: str, token: str | None) -> dict:
        """Inicia otra ronda en la misma sala después de terminar la partida."""
        game = self._game(code)
        players = self._players(game["code"])
        if not any(token and p["token"] == token for p in players):
            raise GameError("No tienes permiso para iniciar otra ronda.", 403)
        if game["status"] != "finished":
            raise GameError("La ronda todavía no ha terminado.", 409)
        now = _now()
        starting_chips = game["initial_chips"] - game["ante"]
        statements = [
            (
                "UPDATE players SET chips=? WHERE game_code=?",
                [starting_chips, game["code"]],
            ),
            (
                "UPDATE games SET status='playing', pot=?, phase='first_roll', "
                "first_roll=NULL, current_seat=0, winner_seat=NULL, finish_reason=NULL, "
                "turn_deadline=?, turn_no=turn_no+1, version=version+1, updated_at=? WHERE code=?",
                [game["ante"] * len(players), time.time() + TURN_SECONDS, now, game["code"]],
            ),
        ]
        self._write(statements)
        return self.state(game["code"], token)

    # ------------------------------------------------------------------
    # Acciones de juego
    # ------------------------------------------------------------------
    def _check_turn(self, game: dict, players: list[dict], token: str | None,
                    version: int | None, expected_phase: str) -> dict:
        if game["status"] != "playing":
            raise GameError("La partida no está en juego.", 409)
        if version is not None and version != game["version"]:
            raise GameError("La mesa cambió, se actualizó el estado.", 409)
        if game["phase"] != expected_phase:
            raise GameError("Esa acción no corresponde a esta fase del turno.", 409)
        player = next(p for p in players if p["seat"] == game["current_seat"])
        if game["mode"] == "online" and player["token"] != token:
            raise GameError("No es tu turno.", 403)
        return player

    def _expire_turn_if_needed(self, game: dict) -> bool:
        if game["status"] != "playing" or not game["turn_deadline"] or game["turn_deadline"] > time.time():
            return False
        players = self._players(game["code"])
        player = next((p for p in players if p["seat"] == game["current_seat"]), None)
        if not player:
            return False
        if game["phase"] == "first_roll":
            msg = f"{player['name']} agotó sus 15 segundos y pierde el turno sin lanzar."
            first, second, bet = None, None, 0
        elif game["phase"] == "bet":
            msg = f"{player['name']} agotó sus 15 segundos y pasa sin completar la apuesta."
            first, second, bet = game["first_roll"], None, 0
        else:
            return False
        self._write(self._turn_statements(
            game, players, player, game["pot"], player["chips"],
            kind="timeout", first=first, second=second, bet=bet, msg=msg
        ))
        return True

    def roll(self, code: str, token: str | None, version: int | None) -> dict:
        """Primer lanzamiento del turno."""
        game = self._game(code)
        if self._expire_turn_if_needed(game):
            raise GameError("Se agotó el tiempo: el turno pasó al siguiente jugador.", 409)
        players = self._players(game["code"])
        player = self._check_turn(game, players, token, version, "first_roll")

        roll = engine.roll_die()
        outcome = engine.resolve_first_roll(roll, game["pot"], player["chips"])
        now = _now()

        # Sale 2-5: el jugador debe decidir su apuesta.
        if not outcome.ends_turn:
            self._write(
                [
                    (
                        "UPDATE games SET phase='bet', first_roll=?, version=version+1, updated_at=? "
                        "WHERE code=?",
                        [roll, now, game["code"]],
                    )
                ]
            )
            event = {"kind": "apuesta", "first_roll": roll, "second_roll": None,
                     "player": player["name"],
                     "message": f"{player['name']} sacó {roll}. Ahora decide cuánto apuesta."}
            return {"state": self.state(game["code"], token), "event": event}

        # Sale 1 o 6: se aplica y pasa el turno.
        new_pot = game["pot"] + outcome.pot_delta
        new_chips = player["chips"] + outcome.chips_delta
        if outcome.kind == "pone":
            msg = f"{player['name']} sacó 1: pone una ficha en el pozo y pierde el turno."
        else:
            msg = (f"{player['name']} sacó 6: saca una ficha del pozo y pierde el turno."
                   if outcome.pot_delta else
                   f"{player['name']} sacó 6, pero el pozo está vacío.")
        stmts = self._turn_statements(game, players, player, new_pot, new_chips,
                                      kind=outcome.kind, first=roll, second=None, bet=0, msg=msg)
        self._write(stmts)
        event = {"kind": outcome.kind, "first_roll": roll, "second_roll": None,
                 "player": player["name"], "message": msg}
        return {"state": self.state(game["code"], token), "event": event}

    def bet(self, code: str, token: str | None, amount: int, version: int | None) -> dict:
        """Apuesta (o pasa con 0) y segundo lanzamiento."""
        game = self._game(code)
        if self._expire_turn_if_needed(game):
            raise GameError("Se agotó el tiempo: el turno pasó al siguiente jugador.", 409)
        players = self._players(game["code"])
        player = self._check_turn(game, players, token, version, "bet")

        error = engine.validate_bet(amount, game["pot"], player["chips"])
        if error:
            raise GameError(error)

        first = game["first_roll"]
        if amount == 0:
            msg = f"{player['name']} sacó {first} y decide pasar sin apostar."
            stmts = self._turn_statements(game, players, player, game["pot"], player["chips"],
                                          kind="pasa", first=first, second=None, bet=0, msg=msg)
            self._write(stmts)
            event = {"kind": "pasa", "first_roll": first, "second_roll": None,
                     "player": player["name"], "message": msg}
            return {"state": self.state(game["code"], token), "event": event}

        second = engine.roll_die()
        result = engine.resolve_bet(first, second, amount)
        new_pot = game["pot"] + result.pot_delta
        new_chips = player["chips"] + result.chips_delta
        if result.won:
            kind = "gana"
            msg = (f"{player['name']} apostó {amount}: sacó {first} y luego {second}. "
                   f"¡Gana {amount} del pozo!")
        else:
            kind = "pierde"
            why = "igual" if second == first else "menor"
            msg = (f"{player['name']} apostó {amount}: sacó {first} y luego {second} ({why}). "
                   f"Pierde {amount}, que van al pozo.")
        stmts = self._turn_statements(game, players, player, new_pot, new_chips,
                                      kind=kind, first=first, second=second, bet=amount, msg=msg)
        self._write(stmts)
        event = {"kind": kind, "first_roll": first, "second_roll": second,
                 "player": player["name"], "message": msg}
        return {"state": self.state(game["code"], token), "event": event}

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------
    def _turn_statements(self, game: dict, players: list[dict], player: dict, new_pot: int,
                         new_chips: int, *, kind: str, first: int | None, second: int | None,
                         bet: int, msg: str) -> list[tuple[str, list[Any]]]:
        """Arma todas las sentencias que cierran un turno (se ejecutan en una transacción)."""
        now = _now()
        chips_by_seat = {p["seat"]: p["chips"] for p in players}
        chips_by_seat[player["seat"]] = new_chips

        reason = engine.game_over_reason(new_pot, chips_by_seat)
        if reason:
            status, phase = "finished", "done"
            winner = engine.pick_winner(chips_by_seat, player["seat"])
            next_seat = game["current_seat"]
            deadline = None
        else:
            status, phase, winner = "playing", "first_roll", None
            next_seat = engine.next_seat(player["seat"], chips_by_seat)
            deadline = time.time() + TURN_SECONDS

        return [
            ("UPDATE players SET chips=? WHERE game_code=? AND seat=?",
             [new_chips, game["code"], player["seat"]]),
            ("INSERT INTO moves (game_code, turn_no, seat, player_name, kind, first_roll, "
             "second_roll, bet, pot_after, chips_after, message, created_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             [game["code"], game["turn_no"] + 1, player["seat"], player["name"], kind, first,
              second, bet, new_pot, new_chips, msg, now]),
            ("UPDATE games SET pot=?, status=?, phase=?, first_roll=NULL, current_seat=?, "
             "winner_seat=?, finish_reason=?, turn_deadline=?, turn_no=turn_no+1, "
             "version=version+1, updated_at=? "
             "WHERE code=?",
             [new_pot, status, phase, next_seat, winner, reason, deadline, now, game["code"]]),
        ]

    def _write(self, stmts: list[tuple[str, list[Any]]], conflict_msg: str | None = None) -> None:
        try:
            self.db.batch(stmts)
        except DBError as exc:
            if conflict_msg and "UNIQUE" in str(exc).upper():
                raise GameError(conflict_msg, 409) from exc
            raise
