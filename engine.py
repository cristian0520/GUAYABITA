"""
Reglas de La Guayabita (funciones puras, sin base de datos ni web).

Resumen de las reglas implementadas:
  * Cada jugador pone la "apuesta inicial" (ante) en el pozo.
  * En su turno el jugador lanza el dado:
      - 1 -> pone 1 ficha en el pozo y pierde el turno.
      - 6 -> saca 1 ficha del pozo y pierde el turno.
      - 2,3,4,5 -> decide cuánto apuesta (de 1 hasta el máximo permitido)
                   o pasa (apuesta 0).
  * Si apuesta, lanza de nuevo: debe sacar un número ESTRICTAMENTE mayor
    al primero. Si lo logra se lleva lo apostado del pozo; si no (igual o
    menor) lo apostado pasa al pozo.
  * Apuesta máxima = lo que haya en el pozo o las fichas del jugador,
    lo que sea menor (el pozo debe poder pagar y el jugador debe poder pagar).
  * La partida termina cuando el pozo queda vacío o solo queda un jugador
    con fichas. Gana quien tenga más fichas.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

FACES = 6
LOSE_TURN_PUT = 1   # sale 1 -> pone una ficha
LOSE_TURN_TAKE = 6  # sale 6 -> saca una ficha
BET_FACES = (2, 3, 4, 5)


def roll_die() -> int:
    """Dado justo de 6 caras usando el generador criptográfico del sistema."""
    return secrets.randbelow(FACES) + 1


@dataclass(frozen=True)
class FirstRollOutcome:
    kind: str          # "pone" | "saca" | "apuesta"
    chips_delta: int   # cambio en las fichas del jugador
    pot_delta: int     # cambio en el pozo
    ends_turn: bool


def resolve_first_roll(roll: int, pot: int, chips: int) -> FirstRollOutcome:
    if roll == LOSE_TURN_PUT:
        amount = min(1, chips)
        return FirstRollOutcome("pone", -amount, +amount, True)
    if roll == LOSE_TURN_TAKE:
        amount = min(1, pot)  # si el pozo se está vaciando, se lleva lo que quede
        return FirstRollOutcome("saca", +amount, -amount, True)
    return FirstRollOutcome("apuesta", 0, 0, False)


def max_bet(pot: int, chips: int) -> int:
    return max(0, min(pot, chips))


def validate_bet(bet: int, pot: int, chips: int) -> str | None:
    """Devuelve un mensaje de error o None si la apuesta es válida (0 = pasar)."""
    if bet < 0:
        return "La apuesta no puede ser negativa."
    limit = max_bet(pot, chips)
    if bet > limit:
        return f"La apuesta máxima ahora es {limit}."
    return None


@dataclass(frozen=True)
class BetOutcome:
    won: bool
    chips_delta: int
    pot_delta: int


def resolve_bet(first_roll: int, second_roll: int, bet: int) -> BetOutcome:
    won = second_roll > first_roll  # estrictamente mayor
    if won:
        return BetOutcome(True, +bet, -bet)
    return BetOutcome(False, -bet, +bet)


def next_seat(current: int, chips_by_seat: dict[int, int]) -> int:
    """Siguiente asiento (en orden circular) que aún tenga fichas."""
    seats = sorted(chips_by_seat)
    n = len(seats)
    start = seats.index(current)
    for step in range(1, n + 1):
        candidate = seats[(start + step) % n]
        if chips_by_seat[candidate] > 0:
            return candidate
    return current


def game_over_reason(pot: int, chips_by_seat: dict[int, int]) -> str | None:
    if pot <= 0:
        return "pozo_vacio"
    if sum(1 for c in chips_by_seat.values() if c > 0) <= 1:
        return "ultimo_jugador"
    return None


def pick_winner(chips_by_seat: dict[int, int], last_actor: int) -> int:
    """Quien tenga más fichas. En empate gana quien hizo la última jugada."""
    best = max(chips_by_seat.values())
    tied = sorted(s for s, c in chips_by_seat.items() if c == best)
    return last_actor if last_actor in tied else tied[0]
