# EV Bot Pro v8.3 — AI-Powered

## Nuevas funciones vs v8.2

### Motor (ev_engine.py)
- **Claude AI por partido** — busca lesiones, forma reciente y H2H en web en tiempo real
- **3 Jugadas de Oro** — los picks con mayor EV y validación del día
- **Picks del Día** — top 5 picks jugables
- **Player Props** — MLB (hits/HR/Ks), NBA (pts/reb/ast), NHL (goles/shots)
- **Parlay 3 y Parlay 6** — reemplaza los tickets anteriores
- **Análisis completo por partido** — ML + Spread + Total con % en cada uno

### App (app.py)
- Ruta /health actualizada a v8.3

### Frontend (index.html)
- Sección 💎 3 Jugadas de Oro
- Sección 🎯 Picks del Día
- Sección 📈 Props Destacados
- Sección 🎟️ Parlays (3 y 6)
- Análisis IA por partido con caja visual

## Variables de entorno necesarias
```
ODDS_API_KEY=tu_odds_api_key
ANTHROPIC_API_KEY=tu_anthropic_api_key
REGIONS=us
MARKETS=h2h,spreads,totals
LOCAL_TZ=America/New_York
ONLY_TODAY=1
CACHE_TTL=900
PROPS_CACHE_TTL=1800
```

## Instrucciones de deploy
1. Renombra ev_engine_v3.py → ev_engine.py (reemplaza el anterior)
2. Reemplaza app.py con app_v3.py
3. Copia index_v3.html → templates/index.html
4. Agrega al style.css el contenido de style_v3.css
5. Agrega ANTHROPIC_API_KEY en Render Environment
6. Deploy
