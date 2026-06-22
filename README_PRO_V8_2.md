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

## Variables de entorno necesarias
```
ODDS_API_KEY=tu_api_key
REGIONS=us
MARKETS=h2h,spreads,totals
LOCAL_TZ=America/New_York
ONLY_TODAY=1
CACHE_TTL=900
```
