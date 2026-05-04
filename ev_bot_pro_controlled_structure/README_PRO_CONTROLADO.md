# EV Bot Pro Controlado

- Solo MLB/NBA/NHL.
- Solo Moneyline (h2h) para ahorrar créditos.
- Cache 15 minutos.
- Botón Buscar picks = usa cache.
- Botón Actualizar odds = consume créditos.

Render:
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app

Environment:
ODDS_API_KEY=tu_key
CACHE_TTL=900
