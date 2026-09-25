"""
La Guayabita - servidor web (FastAPI).

Ejecutar en local:   uvicorn main:app --reload
Producción (Render): uvicorn main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import Database, DBError
from service import GameError, GameService

try:  # .env solo se usa en local; en producción se usan variables del hosting
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("guayabita")
BASE_DIR = Path(__file__).parent

db = Database()
service = GameService(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_schema()  # crea las tablas si no existen (idempotente)
    log.warning("Base de datos lista (%s)", db.kind)
    yield


app = FastAPI(title="La Guayabita", lifespan=lifespan)


# ---------------------------------------------------------------- errores
@app.exception_handler(GameError)
async def game_error_handler(_: Request, exc: GameError):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(DBError)
async def db_error_handler(_: Request, exc: DBError):
    log.error("Error de base de datos: %s", exc)
    return JSONResponse(
        {"detail": "Error con la base de datos. Intenta de nuevo en unos segundos."},
        status_code=503,
    )


# ---------------------------------------------------------------- modelos
class CreateGameBody(BaseModel):
    mode: str = "online"                    # "local" (un dispositivo) | "online"
    host_name: str = ""                     # modo online
    names: list[str] = Field(default_factory=list)  # modo local
    ante: int = Field(5, ge=1, le=10_000)
    initial_chips: int = Field(50, ge=2, le=1_000_000)


class JoinBody(BaseModel):
    name: str


class TokenBody(BaseModel):
    token: str | None = None


class ActionBody(BaseModel):
    token: str | None = None
    version: int | None = None


class BetBody(ActionBody):
    amount: int = Field(0, ge=0)


class RechargeBody(ActionBody):
    seat: int | None = None
    amount: int = Field(..., ge=1, le=1_000_000)


# ---------------------------------------------------------------- rutas
@app.get("/health")
def health():
    db.execute("SELECT 1")  # comprueba que la base de datos responde
    return {"ok": True, "db": db.kind}


@app.post("/api/games")
def create_game(body: CreateGameBody):
    return service.create_game(body.mode, body.ante, body.initial_chips,
                               host_name=body.host_name, names=body.names)


@app.get("/api/games/{code}")
def get_game(code: str, token: str | None = None):
    return service.state(code, token)


@app.post("/api/games/{code}/join")
def join_game(code: str, body: JoinBody):
    return service.join(code, body.name)


@app.post("/api/games/{code}/start")
def start_game(code: str, body: TokenBody):
    return service.start(code, body.token)


@app.post("/api/games/{code}/roll")
def roll(code: str, body: ActionBody):
    return service.roll(code, body.token, body.version)


@app.post("/api/games/{code}/bet")
def bet(code: str, body: BetBody):
    return service.bet(code, body.token, body.amount, body.version)


@app.post("/api/games/{code}/recharge")
def recharge(code: str, body: RechargeBody):
    return service.recharge(code, body.token, body.seat, body.amount)


# ---------------------------------------------------------------- frontend
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")
