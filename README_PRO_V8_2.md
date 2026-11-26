# EV Bot Pro v8.2

## Cambios respecto a v8.1

### ev_engine.py
- Caché thread-safe con `threading.Lock` (seguro en Gunicorn multi-worker)
- Logging estructurado con `logging` — errores visibles en Render logs
- `smooth_validation` descompuesta en componentes testeables
- `best_price_filtered` registra warning cuando usa precio fallback
- `alt_market_signals` aplica corrección de vig real para Over/Under
- `TICKET_CONFIGS` elimina magic strings en la lógica de tickets
- Constantes de clamp centralizadas (`EV_CLAMP`, `PROB_CLAMP`, etc.)

### app.py
- Logging en todas las rutas
- Ruta `/health` para monitoreo en Render
- Sports default toma `list(SPORTS.keys())` en lugar de lista hardcodeada
- `exc_info=True` en errores para stack traces completos

### index.html
- Versión actualizada a v8.2
- `qs()` helper reemplaza repetición de `document.querySelector`
- Fechas de inicio de partidos formateadas con `toLocaleString`
- Warnings del engine mostrados en el resumen
- Contador de juegos con plural correcto
- `book_count` mostrado en tarjetas de pick
- JS más legible con indentación y comentarios por sección

### requirements.txt
- Agregado `tzdata` (necesario en Render/Linux para `zoneinfo`)

## Variables de entorno necesarias (v8.7)
```
ODDS_API_KEY=tu_api_key
MARKETS=h2h,spreads,totals
LOCAL_TZ=America/New_York
ONLY_TODAY=1

# Actualización y presupuesto de créditos (The Odds API)
REFRESH_INTERVAL_MIN=45        # cada cuánto se re-piden cuotas (mínimo 20). Reemplaza CACHE_TTL.
BILLING_RESET_DAY=             # día del mes en que se reinicia la cuota (1-31). Vacío = cupo fijo de 600/día.
MONTHLY_CREDIT_BUDGET=18000    # meta de consumo mensual
PLAN_CREDITS=20000             # créditos del plan
MIN_FORCE_REFRESH_MIN=10       # "Actualizar" no vuelve a pedir una liga con cuotas más nuevas que esto
BUDGET_GUARD=1                 # 0 = desactiva el cupo diario (modo ahorro)

# Opcionales
LEAGUES=                       # vacío = todas; ej. NFL,NBA,NHL,EPL
BOOKMAKERS=pinnacle,fanduel,draftkings,betmgm,williamhill_us,fanatics,betrivers,lowvig,betonlineag
BETTABLE_BOOKS=fanduel,draftkings,betmgm,williamhill_us,caesars,fanatics,betrivers,bet365
HIGH_ODDS_MIN=150
HIGH_ODDS_MAX=500
HIGH_ODDS_MIN_EDGE=1.0         # edge mínimo (puntos de probabilidad) para Cuotas Altas
TOP_PLAYS_MAX_ODDS=500         # Mejores Jugadas nunca muestra cuotas mayores
```

## v8.8 — selector de liga e Historial
- Selector de liga en el encabezado (se recuerda en el navegador). Elegir una
  liga no cuesta créditos extra: solo se piden cuotas de esa liga. Las ligas
  sin juegos hoy se marcan usando /sports y /events (gratis).
- Pestaña 📜 Historial (`history.py`, `GET /api/history`): guarda cada pick
  de Mejores Jugadas, SÓLIDO, PROBABLE y Cuotas Altas la primera vez que
  aparece (cuota, casa, EV, edge) y lo califica con GET /scores. Resumen de
  ganadas/perdidas, acierto, ganancia por $10 y ROI, por nivel y por liga.
  "Mejores Jugadas" solo se marca cuando el dashboard se calculó con todas
  las ligas.
- Créditos: guardar = 0. Calificar = 2 créditos por liga con picks
  pendientes, como máximo una vez cada 48 h, y nunca con menos de 100
  créditos en la cuenta. Un pick sin marcador dentro de 3 días queda
  "Sin resultado".
- CLV implementado pero apagado (`CLV_ENABLED=false`): con CLV activo pide la
  cuota de Pinnacle de cada partido+mercado con picks una vez en los 60 min
  previos al inicio (1 crédito cada uno).

### Base de datos (Neon, gratis)
1. https://neon.tech → crear cuenta → **New Project** (región cercana a la
   del servicio de Render, p. ej. AWS US East).
2. En el panel del proyecto: **Connect** → copiar la *connection string*
   (`postgresql://usuario:clave@ep-....neon.tech/neondb?sslmode=require`).
3. Render → servicio → **Environment** → agregar `DATABASE_URL` con ese valor
   → guardar (Render redeploya solo).
4. Las tablas se crean solas en la primera carga. Verificar en Historial que
   no aparezca el aviso de "SQLite local".

Sin `DATABASE_URL` se usa SQLite en `history.db` (sirve en local; en Render
se borra en cada deploy).

### Variables nuevas (opcionales)
```
DATABASE_URL=                  # Postgres (Neon). Vacío = SQLite local
HISTORY_ENABLED=1              # 0 = no guardar ni calificar picks
SCORES_INTERVAL_H=48           # horas entre consultas a /scores por liga
HISTORY_MIN_REMAINING=100      # no calificar ni pedir CLV con menos créditos
GRADE_DELAY_H=4                # esperar estas horas desde el inicio antes de calificar
CLV_ENABLED=false              # cuotas de cierre de Pinnacle (1 crédito por partido+mercado)
CLV_WINDOW_MIN=60              # ventana previa al inicio para tomar la cuota de cierre
```

## v8.7.1
- PROBABLE respeta el piso de probabilidad (55%). Un EV positivo por debajo
  del piso es "💰 CUOTA ALTA" y solo aparece en Cuotas Altas (no en
  SÓLIDO/PROBABLE/EVITAR ni en Mejores Jugadas).
- Cuota ≥ +150 sin Pinnacle como ancla: aviso "⚠ Sin ancla sharp" y fuera de
  Mejores Jugadas y Cuotas Altas. Cuotas Altas exige edge ≥ 1.0 pt.
- Mejores Jugadas: nunca cuotas mayores a +500.
- Guardián de créditos por ritmo sobre `x-requests-used` (lo que reporta la
  API), sin estado propio: sobrevive reinicios de Render. Permitido a hoy =
  presupuesto × día del ciclo / días del ciclo. Una liga frenada por cupo
  se muestra como "⏸ Cupo", no como error.

## v8.7 — multi-liga, Pinnacle y presupuesto de créditos
- Ligas: NFL, NCAAF, NCAAB, NBA, NHL, MLB, EPL, La Liga, Champions, Liga MX,
  Championship, Portugal, Eredivisie. Solo se piden cuotas (`/odds`, cuesta
  créditos) de ligas en temporada (`/sports`) con juegos por empezar hoy
  (`/events`); esos dos endpoints no cuestan créditos.
- Pinnacle entra por el parámetro `bookmakers` (≤10 casas = costo de 1
  región): 3 créditos por liga por actualización. Pinnacle/LowVig/BetOnline
  son solo ancla del precio justo y corroboración; el mejor precio se busca
  únicamente en `BETTABLE_BOOKS` (casas legales en NJ).
- bet365 no está disponible para EE.UU. en The Odds API; Caesars y Fanatics
  solo aparecen con plan pago.
- Nuevas pestañas: ⭐ Mejores Jugadas (top 5, primera pestaña) y 💰 Cuotas
  Altas (+150 a +500 con EV positivo contra Pinnacle, ganancia por $10).
- Contador de créditos en la barra superior (headers `x-requests-*`), aviso
  con menos del 20%, y "modo ahorro" cuando se agota el cupo diario (sirve
  las últimas cuotas guardadas en vez de pedir nuevas).
- `gunicorn.conf.py`: 1 worker (la caché y el contador viven en memoria; con
  varios workers el consumo se multiplica). Verificar que el start command de
  Render no pase `--workers`.
