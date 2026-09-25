"""Pruebas: python -m unittest -v"""
import os
import tempfile
import unittest
from unittest import mock

import engine
from db import Database
from service import GameError, GameService


class EngineTests(unittest.TestCase):
    def test_primer_tiro(self):
        o = engine.resolve_first_roll(1, pot=20, chips=10)
        self.assertEqual((o.kind, o.chips_delta, o.pot_delta, o.ends_turn), ("pone", -1, 1, True))
        o = engine.resolve_first_roll(6, pot=20, chips=10)
        self.assertEqual((o.kind, o.chips_delta, o.pot_delta, o.ends_turn), ("saca", 1, -1, True))
        for r in (2, 3, 4, 5):
            self.assertFalse(engine.resolve_first_roll(r, 20, 10).ends_turn)

    def test_seis_con_pozo_casi_vacio(self):
        o = engine.resolve_first_roll(6, pot=0, chips=10)
        self.assertEqual((o.chips_delta, o.pot_delta), (0, 0))

    def test_segundo_tiro_estrictamente_mayor(self):
        self.assertTrue(engine.resolve_bet(3, 4, 5).won)
        self.assertFalse(engine.resolve_bet(3, 3, 5).won)   # igual pierde
        self.assertFalse(engine.resolve_bet(3, 2, 5).won)
        self.assertEqual(engine.resolve_bet(3, 6, 5).chips_delta, 5)
        self.assertEqual(engine.resolve_bet(3, 1, 5).pot_delta, 5)

    def test_apuesta_maxima(self):
        self.assertEqual(engine.max_bet(pot=30, chips=12), 12)
        self.assertEqual(engine.max_bet(pot=8, chips=40), 8)
        self.assertIsNotNone(engine.validate_bet(9, 8, 40))
        self.assertIsNone(engine.validate_bet(0, 8, 40))
        self.assertIsNone(engine.validate_bet(8, 8, 40))

    def test_turnos_saltan_eliminados(self):
        self.assertEqual(engine.next_seat(0, {0: 5, 1: 0, 2: 3}), 2)
        self.assertEqual(engine.next_seat(2, {0: 5, 1: 0, 2: 3}), 0)

    def test_fin_de_partida(self):
        self.assertEqual(engine.game_over_reason(0, {0: 5, 1: 5}), "pozo_vacio")
        self.assertEqual(engine.game_over_reason(9, {0: 5, 1: 0}), "ultimo_jugador")
        self.assertIsNone(engine.game_over_reason(9, {0: 5, 1: 1}))
        self.assertEqual(engine.pick_winner({0: 10, 1: 10, 2: 3}, last_actor=1), 1)
        self.assertEqual(engine.pick_winner({0: 10, 1: 12}, last_actor=0), 1)

    def test_dado_justo_rango(self):
        seen = {engine.roll_die() for _ in range(500)}
        self.assertEqual(seen, {1, 2, 3, 4, 5, 6})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        with mock.patch.dict(os.environ, {"LOCAL_DB_PATH": self.tmp.name, "TURSO_DATABASE_URL": ""}):
            self.db = Database()
        self.db.init_schema()
        self.svc = GameService(self.db)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def dice(self, *values):
        return mock.patch("engine.roll_die", side_effect=list(values))

    def local_game(self, names=("Ana", "Beto", "Cami"), ante=5, chips=50):
        return self.svc.create_game("local", ante, chips, names=list(names))

    # -- creación -------------------------------------------------------
    def test_crear_local_arma_pozo_y_fichas(self):
        g = self.local_game()
        s = self.svc.state(g["code"], g["token"])
        self.assertEqual(s["pot"], 15)                       # 3 x 5
        self.assertEqual([p["chips"] for p in s["players"]], [45, 45, 45])
        self.assertEqual((s["status"], s["phase"], s["current_seat"]), ("playing", "first_roll", 0))

    def test_validaciones_de_creacion(self):
        with self.assertRaises(GameError):
            self.svc.create_game("local", 5, 50, names=["Solo"])
        with self.assertRaises(GameError):
            self.svc.create_game("local", 5, 50, names=["Ana", "ana"])
        with self.assertRaises(GameError):
            self.svc.create_game("local", 50, 50, names=["Ana", "Beto"])

    # -- primer tiro ----------------------------------------------------
    def test_sale_uno_pone_ficha_y_pasa_turno(self):
        g = self.local_game()
        with self.dice(1):
            r = self.svc.roll(g["code"], None, None)
        s = r["state"]
        self.assertEqual(s["pot"], 16)
        self.assertEqual(s["players"][0]["chips"], 44)
        self.assertEqual(s["current_seat"], 1)

    def test_sale_seis_saca_ficha(self):
        g = self.local_game()
        with self.dice(6):
            s = self.svc.roll(g["code"], None, None)["state"]
        self.assertEqual(s["pot"], 14)
        self.assertEqual(s["players"][0]["chips"], 46)
        self.assertEqual(s["current_seat"], 1)

    # -- apuestas -------------------------------------------------------
    def test_apuesta_ganada(self):
        g = self.local_game()
        with self.dice(3, 5):
            self.svc.roll(g["code"], None, None)
            s = self.svc.state(g["code"])
            self.assertEqual((s["phase"], s["first_roll"], s["max_bet"]), ("bet", 3, 15))
            r = self.svc.bet(g["code"], None, 10, None)
        s = r["state"]
        self.assertEqual(r["event"]["kind"], "gana")
        self.assertEqual(s["pot"], 5)
        self.assertEqual(s["players"][0]["chips"], 55)
        self.assertEqual(s["current_seat"], 1)

    def test_apuesta_perdida_por_igual(self):
        g = self.local_game()
        with self.dice(4, 4):
            self.svc.roll(g["code"], None, None)
            s = self.svc.bet(g["code"], None, 7, None)["state"]
        self.assertEqual(s["pot"], 22)
        self.assertEqual(s["players"][0]["chips"], 38)

    def test_pasar_no_cambia_nada(self):
        g = self.local_game()
        with self.dice(2):
            self.svc.roll(g["code"], None, None)
        r = self.svc.bet(g["code"], None, 0, None)
        self.assertEqual(r["event"]["kind"], "pasa")
        self.assertEqual(r["state"]["pot"], 15)
        self.assertEqual(r["state"]["current_seat"], 1)

    def test_apuesta_sobre_el_maximo_se_rechaza(self):
        g = self.local_game()
        with self.dice(2):
            self.svc.roll(g["code"], None, None)
        with self.assertRaises(GameError):
            self.svc.bet(g["code"], None, 16, None)   # pozo = 15

    def test_fase_incorrecta_y_version(self):
        g = self.local_game()
        with self.assertRaises(GameError):
            self.svc.bet(g["code"], None, 1, None)     # aún no hay primer tiro
        with self.assertRaises(GameError) as ctx, self.dice(1):
            self.svc.roll(g["code"], None, 999)        # versión vieja
        self.assertEqual(ctx.exception.status, 409)

    # -- fin de partida -------------------------------------------------
    def test_pozo_vacio_termina_la_ronda(self):
        g = self.local_game(names=("Ana", "Beto"), ante=5, chips=50)   # pozo 10
        with self.dice(2, 6):
            self.svc.roll(g["code"], None, None)
            s = self.svc.bet(g["code"], None, 10, None)["state"]        # se lleva todo
        self.assertEqual(s["pot"], 0)
        self.assertEqual(s["status"], "finished")
        self.assertEqual(s["finish_reason"], "pozo_vacio")
        self.assertEqual(s["winner_seat"], 0)
        self.assertEqual(s["players"][0]["chips"], 55)

    def test_jugador_sin_fichas_queda_eliminado(self):
        g = self.local_game(names=("Ana", "Beto", "Cami"), ante=5, chips=6)  # 1 ficha c/u, pozo 15
        with self.dice(1):
            s = self.svc.roll(g["code"], None, None)["state"]    # Ana pone su última ficha
        self.assertTrue(s["players"][0]["out"])
        self.assertEqual((s["status"], s["current_seat"]), ("playing", 1))
        with self.dice(1):
            s = self.svc.roll(g["code"], None, None)["state"]    # Beto también: solo queda Cami
        self.assertEqual(s["status"], "finished")
        self.assertEqual(s["finish_reason"], "ultimo_jugador")
        self.assertEqual(s["winner_seat"], 2)

    def test_jugador_sin_saldo_puede_recargar_sin_reiniciar(self):
        g = self.local_game(names=("Ana", "Beto", "Cami"), ante=5, chips=6)
        with self.dice(1):
            state = self.svc.roll(g["code"], None, None)["state"]
        self.assertTrue(state["players"][0]["out"])
        self.assertEqual(state["status"], "playing")

        state = self.svc.recharge(g["code"], g["token"], 0, 20)
        self.assertEqual(state["status"], "playing")
        self.assertEqual(state["players"][0]["chips"], 20)
        self.assertEqual(state["current_seat"], 1)

    def test_turno_salta_al_eliminado(self):
        g = self.local_game(names=("Ana", "Beto", "Cami"), ante=5, chips=6)
        with self.dice(1):
            self.svc.roll(g["code"], None, None)                 # Ana queda en 0
        with self.dice(6):
            s = self.svc.roll(g["code"], None, None)["state"]    # Beto saca ficha y pasa a Cami
        self.assertEqual(s["current_seat"], 2)
        with self.dice(6):
            s = self.svc.roll(g["code"], None, None)["state"]    # Cami -> debe saltar a Beto (Ana out)
        self.assertEqual(s["current_seat"], 1)

    # -- modo online ----------------------------------------------------
    def test_flujo_online_completo(self):
        host = self.svc.create_game("online", 5, 50, host_name="Ana")
        code = host["code"]
        beto = self.svc.join(code, "Beto")
        with self.assertRaises(GameError):
            self.svc.join(code, "beto")                            # nombre repetido
        with self.assertRaises(GameError):
            self.svc.start(code, beto["token"])                    # solo el anfitrión
        s = self.svc.start(code, host["token"])
        self.assertEqual((s["status"], s["pot"]), ("playing", 10))
        with self.assertRaises(GameError):
            self.svc.join(code, "Cami")                            # ya empezó

        with self.assertRaises(GameError) as ctx:
            self.svc.roll(code, beto["token"], None)               # no es su turno
        self.assertEqual(ctx.exception.status, 403)

        with self.dice(1):
            r = self.svc.roll(code, host["token"], None)
        self.assertEqual(r["state"]["current_seat"], 1)
        self.assertFalse(r["state"]["can_act"])                    # Ana ya no puede actuar
        self.assertTrue(self.svc.state(code, beto["token"])["can_act"])

    def test_estado_no_filtra_tokens(self):
        g = self.local_game()
        self.assertNotIn("token", str(self.svc.state(g["code"], g["token"])["players"]))

    def test_codigo_inexistente(self):
        with self.assertRaises(GameError) as ctx:
            self.svc.state("ZZZZZ")
        self.assertEqual(ctx.exception.status, 404)


class SimulacionTests(unittest.TestCase):
    """Partidas completas con dados reales: no debe romperse ninguna invariante."""

    def test_partidas_aleatorias(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            with mock.patch.dict(os.environ, {"LOCAL_DB_PATH": tmp.name, "TURSO_DATABASE_URL": ""}):
                db = Database()
            db.init_schema()
            svc = GameService(db)
            import random
            rng = random.Random(7)
            for _ in range(60):
                g = svc.create_game("local", 5, 30, names=["A", "B", "C", "D"])
                s = svc.state(g["code"])
                total = s["pot"] + sum(p["chips"] for p in s["players"])
                for _turn in range(2000):
                    if s["status"] == "finished":
                        break
                    if s["phase"] == "first_roll":
                        s = svc.roll(g["code"], None, s["version"])["state"]
                    else:
                        amount = rng.randint(0, s["max_bet"])
                        s = svc.bet(g["code"], None, amount, s["version"])["state"]
                    # las fichas se conservan salvo cuando el "6" con pozo vacío (nada cambia)
                    self.assertEqual(s["pot"] + sum(p["chips"] for p in s["players"]), total)
                    self.assertGreaterEqual(s["pot"], 0)
                    self.assertTrue(all(p["chips"] >= 0 for p in s["players"]))
                self.assertEqual(s["status"], "finished", "la partida debe terminar")
                self.assertIsNotNone(s["winner_seat"])


if __name__ == "__main__":
    unittest.main()
