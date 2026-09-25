# La Guayabita AXM

Juego de dado y apuestas de Colombia, con partidas guardadas en **Turso**.

* **Backend:** Python + FastAPI
* **Base de datos:** Turso (libSQL) por HTTP, sin compilar nada. Sin credenciales usa SQLite local.
* **Interfaz:** HTML/CSS/JS incluido, funciona en celular y computador.
* **Dos modos:** *un solo dispositivo* (todos en la misma pantalla) u *online* (cada quien entra con un código desde su celular).

## Archivos

| Archivo | Para qué sirve |
|---|---|
| `engine.py` | Reglas del juego (funciones puras) |
| `service.py` | Crear/unirse/iniciar/lanzar/apostar, guarda en la base |
| `db.py` | Conexión a Turso (o SQLite local) y creación de tablas |
| `main.py` | API FastAPI y servidor de la interfaz |
| `static/` | `index.html`, `style.css`, `app.js` |
| `test_game.py` | 23 pruebas automáticas |

Las tablas (`games`, `players`, `moves`, `users`, `auth_sessions`) se crean solas al arrancar.

La aplicación incluye registro e inicio de sesión con usuario y contraseña. Las contraseñas
se almacenan como hashes `scrypt` con salt aleatorio; nunca se guarda la contraseña original.
Los usuarios reciben una sesión temporal y un perfil inicial con nombre visible, avatar y emblema.

## Mantenimiento rápido

Estos son los puntos que normalmente se cambian al personalizar el juego:

| Necesidad | Archivo y ubicación |
|---|---|
| Colores, fuentes y fondo | `static/style.css`, bloque `:root` |
| Tamaño, color y animación de dados | `static/style.css`, bloque `Dados 3D` |
| Tiempo visible de la portada | `static/app.js`, `await sleep(5000)` dentro de `boot()` |
| Duración de la barra de portada | `static/style.css`, `animation: loading-progress 5s` |
| Tiempo real del turno | `service.py`, `TURN_SECONDS = 15` |
| Texto y reglas de la portada | `static/app.js`, función `renderLoading()` |
| Mesa, asientos y fichas del pozo | `static/app.js`, función `renderTable()` y `potHTML()` |
| Chat y burbuja temporal | `static/app.js`, variable `recentChat`; estilos `.chat` y `.table-bubble` |
| Avatares disponibles | `static/app.js` y lista `allowed` en `service.py` |
| Reglas matemáticas del juego | `engine.py` |
| Rutas HTTP | `main.py` |
| Tablas y conexión de datos | `db.py` |

Cuando cambies archivos estáticos, actualiza los valores `?v=...` de `static/index.html`
para evitar que el navegador conserve una versión anterior. Después ejecuta `git diff --check`,
prueba localmente y publica con `git push`.

## 1. Crear la base en Turso

```bash
# Instalar CLI (macOS/Linux). En Windows usa WSL.
curl -sSfL https://get.tur.so/install.sh | bash

turso auth login
turso db create guayabita
turso db show guayabita --url          # -> TURSO_DATABASE_URL
turso db tokens create guayabita       # -> TURSO_AUTH_TOKEN
```

## 2. Probar en tu computador

Necesitas Python 3.10 o superior.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                   # y pega tu URL y token de Turso
uvicorn main:app --reload
```

Abre http://127.0.0.1:8000. Para revisar la conexión: http://127.0.0.1:8000/health
(debe responder `{"ok":true,"db":"turso"}`).

Para probar el modo online tú solo, abre la mesa en una ventana normal y entra a
ella desde una ventana de incógnito (cada navegador guarda su propio jugador).

Pruebas de las reglas: `python -m unittest -v`

## 3. Desplegar (ejemplo con Render, plan gratis)

1. Sube esta carpeta a un repositorio de GitHub (el `.gitignore` ya excluye `.env`).
2. En Render: **New → Web Service** y elige el repositorio.
3. Configura:
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   * **Environment:** `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` y `PYTHON_VERSION` = `3.12.3`
4. Al terminar, abre `https://TU-APP.onrender.com/health`.

El plan gratis de Render se duerme tras un rato sin uso y la primera visita tarda
unos segundos, pero las partidas **no se pierden** porque viven en Turso.
Railway, Fly.io o cualquier hosting con Python sirven igual (incluye un `Procfile`).

## Reglas implementadas

1. Cada jugador pone la **apuesta inicial** en el pozo; el resto son sus fichas.
2. Primer lanzamiento: **1** pone una ficha en el pozo y pierde el turno. **6** saca una ficha del pozo (o lo que quede) y pierde el turno. **2 a 5** permiten apostar.
3. Puede apostar de 1 hasta el **máximo = lo menor entre el pozo y sus fichas**, o **pasar**.
4. Segundo lanzamiento: debe salir un número **estrictamente mayor**. Si gana, se lleva lo apostado del pozo; si sale igual o menor, lo apostado va al pozo.
5. Quien queda sin fichas queda eliminado y se le salta el turno.
6. La ronda termina cuando el pozo queda vacío o solo queda un jugador con fichas. El ganador puede pulsar **Volver a apostar** para iniciar otra ronda en la misma sala, conservando el código y los jugadores.
7. Cada turno dura **15 segundos**. Si el jugador no lanza, apuesta o pasa antes de que termine el plazo, el servidor registra que perdió el turno y pasa automáticamente al siguiente jugador con fichas.

Los puntos 3, 5 y 6 no estaban en tu texto y son decisiones mías; cada variante regional los
juega distinto. Se cambian en `engine.py` (`max_bet`, `game_over_reason`, `pick_winner`).
El dado se lanza en el servidor con `secrets`, así nadie puede hacer trampa desde el navegador.

## Límites de esta primera versión

* Cada jugador online se identifica con un token guardado en su navegador. Si borra los datos del navegador no puede volver a su asiento.
* En el modo de un solo dispositivo cualquiera que tenga el código puede jugar los turnos.
* No hay limpieza automática de partidas viejas ni límite de peticiones (rate limit).
* La pantalla se actualiza consultando al servidor cada 2,5 s (no usa WebSockets).
