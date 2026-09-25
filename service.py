"""
Lógica de partidas: conecta las reglas (engine) con la base de datos (db).
No depende de FastAPI, así que se puede probar por separado.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import engine
from db import Database, DBError

MAX_PLAYERS = 8
MIN_PLAYERS = 2
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O/1/I para evitar confusiones
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
        }

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
                    "version=version+1, updated_at=? WHERE code=?",
                    [game["ante"] * len(players), players[0]["seat"], _now(), game["code"]],
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
                    "current_seat=?, winner_seat=NULL, finish_reason=NULL, "
                    "version=version+1, updated_at=? WHERE code=? AND status='finished'",
                    [target_seat, _now(), game["code"]],
                ),
            ]
        )
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

    def roll(self, code: str, token: str | None, version: int | None) -> dict:
        """Primer lanzamiento del turno."""
        game = self._game(code)
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

        # Una mesa compartida no termina porque se vacíe el pozo o porque
        # quede un solo jugador activo: los demás deben poder continuar.
        reason = "todos_sin_saldo" if not any(chips_by_seat.values()) else None
        if reason:
            status, phase = "finished", "done"
            winner = engine.pick_winner(chips_by_seat, player["seat"])
            next_seat = game["current_seat"]
        else:
            status, phase, winner = "playing", "first_roll", None
            next_seat = engine.next_seat(player["seat"], chips_by_seat)

        return [
            ("UPDATE players SET chips=? WHERE game_code=? AND seat=?",
             [new_chips, game["code"], player["seat"]]),
            ("INSERT INTO moves (game_code, turn_no, seat, player_name, kind, first_roll, "
             "second_roll, bet, pot_after, chips_after, message, created_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
             [game["code"], game["turn_no"] + 1, player["seat"], player["name"], kind, first,
              second, bet, new_pot, new_chips, msg, now]),
            ("UPDATE games SET pot=?, status=?, phase=?, first_roll=NULL, current_seat=?, "
             "winner_seat=?, finish_reason=?, turn_no=turn_no+1, version=version+1, updated_at=? "
             "WHERE code=?",
             [new_pot, status, phase, next_seat, winner, reason, now, game["code"]]),
        ]

    def _write(self, stmts: list[tuple[str, list[Any]]], conflict_msg: str | None = None) -> None:
        try:
            self.db.batch(stmts)
        except DBError as exc:
            if conflict_msg and "UNIQUE" in str(exc).upper():
                raise GameError(conflict_msg, 409) from exc
            raise
