"""
ev_engine_v3.py — EV Engine v8.3
Cambios principales vs v8.2:
  - Integración con Claude API para análisis enriquecido con web search
  - 3 Jugadas de Oro (mayor EV + confianza)
  - Picks del día (máximo 5)
  - Props de jugadores (MLB, NBA, NHL)
  - Parlay 3 y Parlay 6
  - Análisis completo por partido (ML + Spread + Total)
  - Sin picks rojos en secciones principales
"""

import os
import time
import logging
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ev_engine_v3")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY: str = os.getenv("ODDS_API_KEY", "")
ANTHROPIC_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
SPORTS: dict[str, str] = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
}
PLAYER_PROPS_MARKETS: dict[str, str] = {
    "MLB": "batter_hits,batter_home_runs,pitcher_strikeouts",
    "NBA": "player_points,player_rebounds,player_assists",
    "NHL": "player_goals,player_shots_on_goal",
}
REGIONS: str = os.getenv("REGIONS", "us")
MARKETS: str = os.getenv("MARKETS", "h2h,spreads,totals")
ODDS_FORMAT: str = "american"
LOCAL_TZ: str = os.getenv("LOCAL_TZ", "America/New_York")
ONLY_TODAY: bool = os.getenv("ONLY_TODAY", "1") == "1"
CACHE_TTL: int = int(os.getenv("CACHE_TTL", "900"))
PROPS_CACHE_TTL: int = int(os.getenv("PROPS_CACHE_TTL", "1800"))

EV_CLAMP = (-25.0, 25.0)
EDGE_CLAMP = (-20.0, 20.0)
PROB_CLAMP = (1.0, 85.0)
VAL_CLAMP = (1.0, 95.0)

# ---------------------------------------------------------------------------
# Caché thread-safe
# ---------------------------------------------------------------------------
_cache: dict = {}
_cache_lock = threading.Lock()

def clear_cache():
    with _cache_lock:
        _cache.clear()

def _cache_get(key: str, ttl: int):
    with _cache_lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    remaining = ttl - (time.time() - ts)
    if remaining <= 0:
        return None
    return data, int(remaining)

def _cache_set(key: str, data):
    with _cache_lock:
        _cache[key] = (time.time(), data)

# ---------------------------------------------------------------------------
# Utilidades matemáticas
# ---------------------------------------------------------------------------
def clamp(v, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo

def american_to_decimal(odds) -> Optional[float]:
    if odds is None:
        return None
    o = float(odds)
    return 1 + o / 100 if o > 0 else 1 + 100 / abs(o)

def implied_probability_american(odds) -> Optional[float]:
    if odds is None:
        return None
    o = float(odds)
    return 100 / (o + 100) if o > 0 else abs(o) / (abs(o) + 100)

def calculate_ev(prob: float, decimal_odds: float) -> float:
    return prob * decimal_odds - 1

# ---------------------------------------------------------------------------
# API status
# ---------------------------------------------------------------------------
def api_key_status() -> str:
    if not API_KEY:
        return "missing"
    if len(API_KEY) < 10:
        return "too_short"
    return "present"

def anthropic_key_status() -> str:
    if not ANTHROPIC_KEY:
        return "missing"
    return "present"

# ---------------------------------------------------------------------------
# Fetch odds
# ---------------------------------------------------------------------------
def fetch_odds(sport_key: str, force_refresh: bool = False, markets: str = None, ttl: int = None):
    if not API_KEY or API_KEY == "pon_tu_api_key_aqui":
        raise RuntimeError("Falta ODDS_API_KEY en Render Environment")

    use_markets = markets or MARKETS
    use_ttl = ttl or CACHE_TTL
    cache_key = f"{sport_key}:{REGIONS}:{use_markets}:{ODDS_FORMAT}"

    if not force_refresh:
        cached = _cache_get(cache_key, use_ttl)
        if cached:
            data, remaining = cached
            return data, True, remaining

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": use_markets,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": "iso",
    }
    logger.info("Fetching odds para %s markets=%s", sport_key, use_markets)
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"The Odds API error {r.status_code}: {detail}")

    data = r.json()
    _cache_set(cache_key, data)
    return data, False, use_ttl

# ---------------------------------------------------------------------------
# Fetch player props para un game_id
# ---------------------------------------------------------------------------
def fetch_player_props(sport_key: str, game_id: str, prop_markets: str):
    cache_key = f"props:{sport_key}:{game_id}:{prop_markets}"
    cached = _cache_get(cache_key, PROPS_CACHE_TTL)
    if cached:
        data, _ = cached
        return data

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{game_id}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": prop_markets,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": "iso",
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            logger.warning("Props API error %s para %s", r.status_code, game_id)
            return None
        data = r.json()
        _cache_set(cache_key, data)
        return data
    except Exception as e:
        logger.error("Error fetching props: %s", e)
        return None

# ---------------------------------------------------------------------------
# Filtro de juegos
# ---------------------------------------------------------------------------
def is_game_today(game: dict) -> bool:
    if not ONLY_TODAY:
        return True
    s = game.get("commence_time")
    if not s:
        return False
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        z = ZoneInfo(LOCAL_TZ)
        return dt.astimezone(z).date() == datetime.now(z).date()
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Extracción de mercados
# ---------------------------------------------------------------------------
def get_market_outcomes(game: dict, mkey: str) -> list:
    rows = []
    for b in game.get("bookmakers", []):
        title = b.get("title", "Unknown")
        for m in b.get("markets", []):
            if m.get("key") != mkey:
                continue
            for o in m.get("outcomes", []):
                rows.append({
                    "bookmaker": title,
                    "market": mkey,
                    "name": o.get("name"),
                    "price": o.get("price"),
                    "point": o.get("point"),
                    "description": o.get("description"),
                })
    return rows

def best_price_same(outcomes: list, name: str, point=None) -> Optional[dict]:
    best = None
    for o in outcomes:
        if o.get("name") != name:
            continue
        if point is not None and o.get("point") != point:
            continue
        price = o.get("price")
        dec = american_to_decimal(price)
        if price is None or dec is None:
            continue
        if best is None or dec > best["decimal_odds"]:
            best = {
                "bookmaker": o.get("bookmaker", "Unknown"),
                "american_odds": int(price),
                "decimal_odds": dec,
                "point": o.get("point"),
            }
    return best

# ---------------------------------------------------------------------------
# No-vig y probabilidades justas
# ---------------------------------------------------------------------------
def no_vig_h2h_probabilities(outcomes: list) -> tuple:
    by_book: dict = {}
    for o in outcomes:
        name = o.get("name")
        price = o.get("price")
        book = o.get("bookmaker", "Unknown")
        if name is None or price is None:
            continue
        p = implied_probability_american(price)
        if p is not None:
            by_book.setdefault(book, []).append((name, p))

    probs: dict = {}
    counts: dict = {}
    for items in by_book.values():
        if len(items) < 2:
            continue
        total = sum(p for _, p in items)
        if total <= 0:
            continue
        for name, p in items:
            fair = p / total
            probs[name] = probs.get(name, 0) + fair
            counts[name] = counts.get(name, 0) + 1

    result = {n: clamp(v / counts.get(n, 1), 0.01, 0.99) for n, v in probs.items()}
    return result, counts

def best_price_filtered(outcomes: list, name: str, fair: float, point=None) -> Optional[dict]:
    cand = []
    for o in outcomes:
        if o.get("name") != name:
            continue
        if point is not None and o.get("point") != point:
            continue
        price = o.get("price")
        imp = implied_probability_american(price)
        dec = american_to_decimal(price)
        if price is None or imp is None or dec is None:
            continue
        cand.append({
            "bookmaker": o.get("bookmaker", "Unknown"),
            "american_odds": int(price),
            "decimal_odds": dec,
            "point": o.get("point"),
            "diff": abs(imp - fair),
        })
    if not cand:
        return None
    sane = [c for c in cand if c["diff"] <= 0.25]
    if not sane:
        return sorted(cand, key=lambda c: c["diff"])[0]
    return sorted(sane, key=lambda c: c["decimal_odds"], reverse=True)[0]

# ---------------------------------------------------------------------------
# EV, edge y validación
# ---------------------------------------------------------------------------
def safe_ev_edge(fair: float, odds: int) -> tuple:
    dec = american_to_decimal(odds)
    imp = implied_probability_american(odds)
    if dec is None or imp is None:
        return None, None
    ev = round(clamp(calculate_ev(fair, dec) * 100, *EV_CLAMP), 1)
    edge = round(clamp((fair - imp) * 100, *EDGE_CLAMP), 1)
    return ev, edge

def _odds_adjustment(odds: Optional[int]) -> float:
    if odds is None:
        return 0.0
    if odds <= -1000:
        return -18.0
    if odds <= -400:
        return -10.0
    if odds <= -250:
        return -5.0
    if odds <= -150:
        return 1.0
    if odds >= 600:
        return -10.0
    if odds >= 300:
        return -5.0
    return 0.0

def smooth_validation(prob, odds, books, ev=0.0, edge=0.0) -> float:
    prob = clamp(prob, 1, 85)
    ev = clamp(ev or 0.0, -10, 10)
    edge = clamp(edge or 0.0, -8, 8)
    adj = min((books or 0) * 0.6, 5.0)
    adj += _odds_adjustment(odds)
    adj += clamp(ev * 0.35, -3, 4)
    adj += clamp(edge * 0.45, -3, 4)
    return round(clamp(prob + adj, *VAL_CLAMP), 1)

def classify_pick(prob, val, ev, edge, odds) -> tuple:
    ev = ev if ev is not None else 0.0
    edge = edge if edge is not None else 0.0
    if odds is not None and odds <= -400 and val >= 52 and prob >= 48:
        return "blue", "PROBABLE", "Probable ganador, pero cuota demasiado cara.", "0u-0.25u"
    if val >= 68 and prob >= 58 and ev > -3 and edge > -3:
        return "green", "JUGADA DE ORO", "Alta validación con EV positivo.", "0.50u-1u"
    if val >= 52 and prob >= 48:
        return "blue", "PROBABLE", "Probable ganador con valor aceptable.", "0u-0.25u"
    return "red", "EVITAR", "Baja validación o precio desfavorable.", "0u"

def confidence_score(v) -> float:
    return round(clamp((v or 0) / 10, 0.1, 9.9), 1)

def premium_tag(sig: dict) -> str:
    if sig.get("short_market") != "ML":
        return "🎯 Pick alternativo"
    odds = sig.get("odds")
    if odds is not None and odds <= -300:
        return "⚠ Línea cara"
    if sig.get("ev") is not None and sig["ev"] >= 3:
        return "💎 Value Pick"
    if sig.get("validation", 0) >= 68:
        return "🔒 Jugada de Oro"
    return "📌 Probable"

def sharp_warning(sig: dict) -> str:
    odds = sig.get("odds")
    if odds is not None and odds <= -400:
        return "Cuota muy cara; usar stake bajo."
    if sig.get("ev") is not None and sig["ev"] < -5 and sig.get("validation", 0) >= 55:
        return "Probable, pero sin valor fuerte."
    if sig.get("validation", 0) < 52:
        return "No usar en tickets principales."
    return "Sin alerta fuerte."

def enrich(sig: dict) -> dict:
    sig["confidence_score"] = confidence_score(sig.get("validation"))
    sig["premium_tag"] = premium_tag(sig)
    sig["sharp_warning"] = sharp_warning(sig)
    sig["is_bet_recommendation"] = sig.get("color") in ("green", "blue")
    return sig

# ---------------------------------------------------------------------------
# Señales de mercado
# ---------------------------------------------------------------------------
def moneyline_signals(game: dict) -> list:
    outs = get_market_outcomes(game, "h2h")
    fair, counts = no_vig_h2h_probabilities(outs)
    signals = []
    for name, p in fair.items():
        best = best_price_filtered(outs, name, p)
        if not best:
            continue
        odds = best["american_odds"]
        prob = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        val = smooth_validation(prob, odds, counts.get(name, 0), ev, edge)
        color, label, reason, stake = classify_pick(prob, val, ev, edge, odds)
        signals.append(enrich({
            "market": "Moneyline", "short_market": "ML",
            "selection": name, "probability": prob, "validation": val,
            "color": color, "label": label, "reason": reason, "stake": stake,
            "ev": ev, "edge": edge, "odds": odds, "point": None,
            "bookmaker": best["bookmaker"], "book_count": counts.get(name, 0),
            "is_primary": True,
        }))
    return sorted(signals, key=lambda x: (x["validation"], x["probability"]), reverse=True)

def spread_total_signals(game: dict) -> list:
    signals = []
    for key, short in [("spreads", "Spread"), ("totals", "Total")]:
        outs = get_market_outcomes(game, key)
        seen = set()
        for o in outs:
            name = o.get("name")
            point = o.get("point")
            price = o.get("price")
            if name is None or price is None:
                continue
            k = (key, name, point)
            if k in seen:
                continue
            seen.add(k)
            best = best_price_same(outs, name, point)
            if not best:
                continue
            imp = implied_probability_american(best["american_odds"])
            if imp is None:
                continue

            # Corrección de vig
            opposite = "Over" if name == "Under" else ("Under" if name == "Over" else None)
            if opposite:
                opp = best_price_same(outs, opposite, point)
                if opp:
                    opp_imp = implied_probability_american(opp["american_odds"])
                    if opp_imp:
                        total = imp + opp_imp
                        if total > 0:
                            imp = imp / total

            prob = round(clamp(imp * 100, *PROB_CLAMP), 1)
            val = prob
            odds_val = best["american_odds"]
            if -130 <= odds_val <= 120:
                val += 6
            elif abs(odds_val) > 180:
                val -= 8
            val = round(clamp(val, *VAL_CLAMP), 1)
            color = "blue" if val >= 58 else "red"
            label = "PROBABLE" if val >= 58 else "EVITAR"
            reason = "Mercado con precio razonable." if val >= 58 else "Sin suficiente validación."
            title = f"{name} {point}" if key == "totals" else f"{name} {point:+g}"
            signals.append(enrich({
                "market": short, "short_market": short,
                "selection": title, "probability": prob, "validation": val,
                "color": color, "label": label, "reason": reason,
                "stake": "0u-0.25u" if val >= 58 else "0u",
                "ev": None, "edge": None, "odds": odds_val,
                "point": point, "bookmaker": best["bookmaker"], "is_primary": False,
            }))
    return sorted(signals, key=lambda x: x["validation"], reverse=True)

# ---------------------------------------------------------------------------
# Player Props
# ---------------------------------------------------------------------------
def analyze_player_props(sport_key: str, game_id: str, game_name: str) -> list:
    prop_markets = PLAYER_PROPS_MARKETS.get(
        next((k for k, v in SPORTS.items() if v == sport_key), ""), ""
    )
    if not prop_markets:
        return []

    data = fetch_player_props(sport_key, game_id, prop_markets)
    if not data:
        return []

    props = []
    seen = set()
    for bm in data.get("bookmakers", []):
        book = bm.get("title", "Unknown")
        for market in bm.get("markets", []):
            mkey = market.get("key", "")
            for outcome in market.get("outcomes", []):
                player = outcome.get("description", outcome.get("name", ""))
                name = outcome.get("name", "")
                price = outcome.get("price")
                point = outcome.get("point")
                if not player or price is None:
                    continue
                k = (mkey, player, name, point)
                if k in seen:
                    continue
                seen.add(k)
                imp = implied_probability_american(price)
                if imp is None:
                    continue
                prob = round(clamp(imp * 100, *PROB_CLAMP), 1)
                val = prob
                if -130 <= price <= 120:
                    val += 5
                val = round(clamp(val, *VAL_CLAMP), 1)
                if val < 52:
                    continue
                market_label = mkey.replace("_", " ").title()
                selection = f"{player} — {name}{f' {point}' if point else ''}"
                props.append(enrich({
                    "market": market_label, "short_market": "PROP",
                    "selection": selection, "probability": prob, "validation": val,
                    "color": "blue" if val >= 58 else "red",
                    "label": "PROP DESTACADO" if val >= 58 else "EVITAR",
                    "reason": f"Player prop con probabilidad {prob}%.",
                    "stake": "0u-0.25u", "ev": None, "edge": None,
                    "odds": int(price), "point": point,
                    "bookmaker": book, "game": game_name, "is_primary": False,
                }))

    return sorted(props, key=lambda x: x["validation"], reverse=True)[:3]

# ---------------------------------------------------------------------------
# Análisis Claude AI por partido
# ---------------------------------------------------------------------------
def claude_analyze_game(game_name: str, sport: str, ml_signals: list, spread_total: list) -> str:
    if not ANTHROPIC_KEY:
        return "Análisis Claude no disponible (falta ANTHROPIC_API_KEY)."

    cache_key = f"claude:{game_name}:{sport}"
    cached = _cache_get(cache_key, 3600)
    if cached:
        data, _ = cached
        return data

    ml_text = "\n".join([
        f"- {s['selection']}: prob {s['probability']}%, odds {s['odds']}, EV {s['ev']}%, validación {s['validation']}%"
        for s in ml_signals[:2]
    ])
    alt_text = "\n".join([
        f"- {s['selection']}: prob {s['probability']}%, odds {s['odds']}, validación {s['validation']}%"
        for s in spread_total[:3]
    ])

    prompt = f"""Eres un analista experto de apuestas deportivas. Analiza este partido de {sport}:

Partido: {game_name}

Señales Moneyline:
{ml_text}

Señales Spread/Total:
{alt_text}

Busca información actual sobre lesiones, forma reciente, H2H y condiciones del partido.
Luego dame un análisis conciso (máximo 3 oraciones) con:
1. El equipo favorito y por qué
2. El mercado con mejor valor
3. Un consejo de stake

Responde en español, directo y sin rodeos."""

    try:
        headers = {
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        }
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning("Claude API error %s", r.status_code)
            return "Análisis no disponible en este momento."

        data = r.json()
        text = " ".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        if not text:
            text = "Análisis no disponible."
        _cache_set(cache_key, text)
        return text
    except Exception as e:
        logger.error("Claude analyze error: %s", e)
        return "Análisis no disponible en este momento."

# ---------------------------------------------------------------------------
# Predicción completa por partido
# ---------------------------------------------------------------------------
def game_prediction(game: dict, sport: str) -> dict:
    name = f"{game.get('away_team')} vs {game.get('home_team')}"
    ml = moneyline_signals(game)
    alt = spread_total_signals(game)

    # Player props
    game_id = game.get("id", "")
    sport_key = SPORTS.get(sport, "")
    props = analyze_player_props(sport_key, game_id, name) if game_id else []

    # Análisis Claude
    ai_analysis = claude_analyze_game(name, sport, ml, alt)

    # Mejor pick
    primary = ml[0] if ml else None
    secondary = next((s for s in alt if s["color"] != "red"), alt[0] if alt else None)

    # Jugada de oro — mayor validación entre todos
    all_picks = [s for s in ml + alt if s.get("color") == "green"]
    gold_pick = sorted(all_picks, key=lambda x: (x.get("validation", 0), x.get("ev") or 0), reverse=True)[0] if all_picks else primary

    time_str = ""
    s = game.get("commence_time")
    if s:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            z = ZoneInfo(LOCAL_TZ)
            time_str = dt.astimezone(z).strftime("%b %d %I:%M %p")
        except Exception:
            time_str = s

    return {
        "sport": sport,
        "game": name,
        "game_id": game_id,
        "home_team": game.get("home_team"),
        "away_team": game.get("away_team"),
        "start_time": time_str,
        "ai_analysis": ai_analysis,
        "gold_pick": gold_pick,
        "primary_pick": primary,
        "secondary_pick": secondary,
        "ml_signals": ml,
        "alt_signals": alt,
        "player_props": props,
        "all_signals": [s for s in ml + alt if s.get("color") != "red"],
    }

# ---------------------------------------------------------------------------
# Deduplicación y tickets
# ---------------------------------------------------------------------------
def dedupe(items: list) -> list:
    seen = set()
    out = []
    for x in items:
        k = (x.get("game"), x.get("selection"), x.get("short_market"))
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out

def pick_ok(sig: Optional[dict]) -> bool:
    if not sig or sig.get("color") == "red" or sig.get("validation", 0) < 55:
        return False
    odds = sig.get("odds")
    if odds is not None and (odds <= -450 or odds >= 700):
        return False
    return True

def build_parlay(name: str, count: int, pool: list) -> Optional[dict]:
    picks = []
    games_seen = set()
    for s in pool:
        if len(picks) >= count:
            break
        if s.get("game") in games_seen or not pick_ok(s):
            continue
        picks.append(s)
        games_seen.add(s.get("game"))

    if len(picks) < count:
        return None

    comb = 1.0
    avg = 0.0
    for x in picks:
        comb *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg += x.get("validation", 0)
    avg /= len(picks)

    return {
        "name": name,
        "count": count,
        "picks": picks,
        "validation": round(clamp(avg, *VAL_CLAMP), 1),
        "combined_probability": round(clamp(comb * 100, 0.1, 95), 1),
    }

# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------
def get_dashboard(selected_sports: list, force_refresh: bool = False) -> dict:
    dash = {
        "mode": "Pro v8.3 AI-Powered",
        "cache_ttl_seconds": CACHE_TTL,
        "only_today": ONLY_TODAY,
        "timezone": LOCAL_TZ,
        "anthropic_status": anthropic_key_status(),
        "sports": {},
        "games": [],
        "gold_picks": [],       # 3 Jugadas de Oro
        "picks_del_dia": [],    # Top 5 picks
        "player_props": [],     # Props destacados
        "parlay_3": None,
        "parlay_6": None,
        "warnings": [],
    }

    all_gold = []
    all_picks = []
    all_props = []

    for label in selected_sports:
        key = SPORTS.get(label)
        if not key:
            continue
        try:
            games, from_cache, ttl = fetch_odds(key, force_refresh=force_refresh)
            total = len(games)
            games = [g for g in games if is_game_today(g)]
            dash["sports"][label] = {
                "ok": True, "sport_key": key,
                "games_count": len(games), "total_api_games": total,
                "from_cache": from_cache, "cache_seconds_left": ttl,
            }
            for g in games:
                pred = game_prediction(g, label)
                dash["games"].append(pred)

                # Recopilar picks globales
                for sig in pred.get("all_signals", []):
                    sig_copy = {**sig, "sport": label, "game": pred["game"], "start_time": pred["start_time"]}
                    all_picks.append(sig_copy)
                    if sig.get("color") == "green":
                        all_gold.append(sig_copy)

                for prop in pred.get("player_props", []):
                    all_props.append({**prop, "sport": label, "game": pred["game"]})

        except Exception as e:
            logger.error("Error procesando %s: %s", label, e, exc_info=True)
            dash["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
            dash["warnings"].append(f"{label}: {e}")

    # Ordenar y limitar
    sorter = lambda x: (x.get("validation", 0), x.get("ev") or 0, x.get("probability", 0))

    dash["gold_picks"] = dedupe(sorted(all_gold, key=sorter, reverse=True))[:3]
    dash["picks_del_dia"] = dedupe(sorted(all_picks, key=sorter, reverse=True))[:5]
    dash["player_props"] = dedupe(sorted(all_props, key=sorter, reverse=True))[:3]

    # Parlays
    parlay_pool = dedupe(sorted(all_picks, key=sorter, reverse=True))
    dash["parlay_3"] = build_parlay("Parlay 3", 3, parlay_pool)
    dash["parlay_6"] = build_parlay("Parlay 6", 6, parlay_pool)

    return dash

# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def debug_all_sports() -> dict:
    res = {
        "api_key": api_key_status(),
        "anthropic_key": anthropic_key_status(),
        "regions": REGIONS, "markets": MARKETS,
        "cache_ttl_seconds": CACHE_TTL,
        "only_today": ONLY_TODAY, "timezone": LOCAL_TZ,
        "sports": {},
    }
    for label, key in SPORTS.items():
        try:
            games, fc, ttl = fetch_odds(key)
            total = len(games)
            games = [g for g in games if is_game_today(g)]
            res["sports"][label] = {
                "ok": True, "sport_key": key,
                "games_count": len(games), "total_api_games": total,
                "from_cache": fc, "cache_seconds_left": ttl,
                "sample_games": [
                    {"home_team": g.get("home_team"), "away_team": g.get("away_team"),
                     "commence_time": g.get("commence_time"),
                     "bookmakers_count": len(g.get("bookmakers", []))}
                    for g in games[:3]
                ],
            }
        except Exception as e:
            logger.error("debug error %s: %s", label, e)
            res["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
    return res
