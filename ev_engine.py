"""
ev_engine_v8.3.py
- Soccer: ligas disponibles en The Odds API (gratis), filtro de VALUE alto (odds >= +150)
- MLB/NBA/NHL: modo EFICIENCIA - favoritos sólidos, difícil perder
- Dashboard separado por categoría: value_soccer / efficiency_picks
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
logger = logging.getLogger("ev_engine")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY: str = os.getenv("ODDS_API_KEY", "")

# Deportes principales - modo EFICIENCIA (difícil perder)
SPORTS_EFFICIENCY: dict[str, str] = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
}

# Soccer - modo VALUE (odds altos)
SPORTS_SOCCER: dict[str, str] = {
    "MLS":          "soccer_usa_mls",
    "Premier":      "soccer_epl",
    "La Liga":      "soccer_spain_la_liga",
    "Serie A":      "soccer_italy_serie_a",
    "Bundesliga":   "soccer_germany_bundesliga",
    "Ligue 1":      "soccer_france_ligue_1",
    "Brasileirao":  "soccer_brazil_campeonato",
    "Argentina":    "soccer_argentina_primera_division",
    "Mexico":       "soccer_mexico_ligamx",
    "Colombia":     "soccer_colombia_primera_a",
    "Chile":        "soccer_chile_campeonato",
    "Ecuador":      "soccer_ecuador_liga_pro",
    "Peru":         "soccer_peru_primera_division",
    "Venezuela":    "soccer_venezuela_primera_division",
    "UCL":          "soccer_uefa_champs_league",
    "Europa":       "soccer_uefa_europa_league",
}

SPORTS: dict[str, str] = {**SPORTS_EFFICIENCY, **SPORTS_SOCCER}

REGIONS: str     = os.getenv("REGIONS", "us")
MARKETS: str     = os.getenv("MARKETS", "h2h,spreads,totals")
ODDS_FORMAT: str = "american"
LOCAL_TZ: str    = os.getenv("LOCAL_TZ", "America/New_York")
ONLY_TODAY: bool = os.getenv("ONLY_TODAY", "1") == "1"
CACHE_TTL: int   = int(os.getenv("CACHE_TTL", "900"))

# Umbrales
EFFICIENCY_MIN_PROB   = 62.0   # % mínimo para pick eficiente
EFFICIENCY_MIN_VAL    = 65.0   # validación mínima
EFFICIENCY_MAX_ODDS   = -120   # no más caro que -120 en americano para eficiencia
VALUE_MIN_ODDS_AMER   = 130    # +130 o más para soccer value (2.30 decimal)
VALUE_MIN_PROB        = 30.0   # al menos 30% de probabilidad real
VALUE_MIN_EV          = 2.0    # EV% mínimo positivo para soccer

EV_CLAMP    = (-25.0, 25.0)
EDGE_CLAMP  = (-20.0, 20.0)
PROB_CLAMP  = (1.0, 85.0)
VAL_CLAMP   = (1.0, 95.0)

# ---------------------------------------------------------------------------
# Caché thread-safe
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()

def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()

def _cache_get(key: str) -> Optional[tuple[list, int]]:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        remaining = CACHE_TTL - (time.time() - ts)
        if remaining <= 0:
            return None
        return data, int(remaining)

def _cache_set(key: str, data: list) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), data)

# ---------------------------------------------------------------------------
# Matemáticas
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
# API
# ---------------------------------------------------------------------------
def api_key_status() -> str:
    if not API_KEY:
        return "missing"
    if len(API_KEY) < 10:
        return "too_short"
    return "present"

def fetch_odds(sport_key: str, force_refresh: bool = False) -> tuple[list, bool, int]:
    if not API_KEY or API_KEY == "pon_tu_api_key_aqui":
        raise RuntimeError("Falta ODDS_API_KEY en Render Environment")

    cache_key = f"{sport_key}:{REGIONS}:{MARKETS}:{ODDS_FORMAT}"
    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached:
            data, remaining = cached
            return data, True, remaining

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": "iso",
    }
    logger.info("Fetching odds para %s", sport_key)
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"The Odds API error {r.status_code}: {detail}")

    data = r.json()
    _cache_set(cache_key, data)
    return data, False, CACHE_TTL

# ---------------------------------------------------------------------------
# Filtros
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
# Mercados
# ---------------------------------------------------------------------------
def get_market_outcomes(game: dict, mkey: str) -> list[dict]:
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

def no_vig_h2h_probabilities(outcomes: list) -> tuple[dict, dict]:
    by_book: dict[str, list] = {}
    for o in outcomes:
        name = o.get("name")
        price = o.get("price")
        book = o.get("bookmaker", "Unknown")
        if name is None or price is None:
            continue
        p = implied_probability_american(price)
        if p is not None:
            by_book.setdefault(book, []).append((name, p))

    probs: dict[str, float] = {}
    counts: dict[str, int] = {}
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

    result = {
        n: clamp(v / counts.get(n, 1), 0.01, 0.99)
        for n, v in probs.items()
    }
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
        fallback = sorted(cand, key=lambda c: c["diff"])[0]
        return fallback
    return sorted(sane, key=lambda c: c["decimal_odds"], reverse=True)[0]

# ---------------------------------------------------------------------------
# EV y validación
# ---------------------------------------------------------------------------
def safe_ev_edge(fair: float, odds: int) -> tuple[Optional[float], Optional[float]]:
    dec = american_to_decimal(odds)
    imp = implied_probability_american(odds)
    if dec is None or imp is None:
        return None, None
    ev   = round(clamp(calculate_ev(fair, dec) * 100, *EV_CLAMP), 1)
    edge = round(clamp((fair - imp) * 100, *EDGE_CLAMP), 1)
    return ev, edge

def _odds_adjustment(odds: Optional[int]) -> float:
    if odds is None:
        return 0.0
    if odds <= -1000: return -18.0
    if odds <= -400:  return -10.0
    if odds <= -250:  return -5.0
    if odds <= -150:  return 1.0
    if odds >= 600:   return -10.0
    if odds >= 300:   return -5.0
    return 0.0

def smooth_validation(prob, odds, books, ev=0.0, edge=0.0) -> float:
    prob  = clamp(prob, 1, 85)
    ev    = clamp(ev or 0.0, -10, 10)
    edge  = clamp(edge or 0.0, -8, 8)
    adj_books = min((books or 0) * 0.6, 5.0)
    adj_odds  = _odds_adjustment(odds)
    adj_ev    = clamp(ev * 0.35, -3, 4)
    adj_edge  = clamp(edge * 0.45, -3, 4)
    return round(clamp(prob + adj_books + adj_odds + adj_ev + adj_edge, *VAL_CLAMP), 1)

# ---------------------------------------------------------------------------
# Clasificación EFICIENCIA (MLB/NBA/NHL)
# ---------------------------------------------------------------------------
def classify_efficiency(prob, val, ev, edge, odds) -> tuple[str, str, str, str]:
    ev    = ev or 0.0
    edge  = edge or 0.0

    # Favorito sólido con buen precio
    if prob >= EFFICIENCY_MIN_PROB and val >= EFFICIENCY_MIN_VAL and ev >= 0 and edge >= 0:
        return "green", "🔒 SÓLIDO", "Favorito con alta probabilidad y buen valor.", "0.5u-1u"

    # Probable pero sin mucho valor
    if prob >= 58 and val >= 60:
        return "blue", "📌 PROBABLE", "Alta probabilidad pero precio ajustado.", "0.25u"

    return "red", "⚠ EVITAR", "No cumple criterios de eficiencia.", "0u"

# ---------------------------------------------------------------------------
# Clasificación VALUE SOCCER
# ---------------------------------------------------------------------------
def classify_soccer_value(prob, val, ev, edge, odds) -> tuple[str, str, str, str]:
    ev   = ev or 0.0
    edge = edge or 0.0

    # Odds positivos altos con valor real
    if odds >= VALUE_MIN_ODDS_AMER and prob >= VALUE_MIN_PROB and ev >= VALUE_MIN_EV:
        return "gold", "💎 VALUE ALTO", "Odds altos con valor matemático positivo.", "0.5u"

    # Buen value moderado
    if odds >= 100 and prob >= 35 and ev >= 1.0:
        return "silver", "🎯 VALUE", "Cuota positiva con valor aceptable.", "0.25u"

    return "red", "⚠ EVITAR", "Sin valor suficiente.", "0u"

# ---------------------------------------------------------------------------
# Enriquecimiento
# ---------------------------------------------------------------------------
def confidence_score(v) -> float:
    return round(clamp((v or 0) / 10, 0.1, 9.9), 1)

def enrich(sig: dict) -> dict:
    sig["confidence_score"] = confidence_score(sig.get("validation"))
    sig["is_bet_recommendation"] = sig.get("color") in ("green", "blue", "gold", "silver")
    return sig

# ---------------------------------------------------------------------------
# Señales ML - EFICIENCIA
# ---------------------------------------------------------------------------
def moneyline_efficiency(game: dict, sport: str) -> list[dict]:
    outs = get_market_outcomes(game, "h2h")
    fair, counts = no_vig_h2h_probabilities(outs)
    signals = []

    for name, p in fair.items():
        best = best_price_filtered(outs, name, p)
        if not best:
            continue
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        val   = smooth_validation(prob, odds, counts.get(name, 0), ev, edge)
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds)

        signals.append(enrich({
            "mode":        "efficiency",
            "sport":       sport,
            "market":      "Moneyline",
            "short_market":"ML",
            "selection":   name,
            "probability": prob,
            "validation":  val,
            "color":       color,
            "label":       label,
            "reason":      reason,
            "stake":       stake,
            "ev":          ev,
            "edge":        edge,
            "odds":        odds,
            "decimal_odds":best["decimal_odds"],
            "bookmaker":   best["bookmaker"],
            "book_count":  counts.get(name, 0),
            "is_primary":  True,
        }))

    return sorted(signals, key=lambda x: (x["validation"], x["probability"]), reverse=True)

# ---------------------------------------------------------------------------
# Señales ML - VALUE SOCCER
# ---------------------------------------------------------------------------
def moneyline_soccer_value(game: dict, sport: str) -> list[dict]:
    outs  = get_market_outcomes(game, "h2h")
    fair, counts = no_vig_h2h_probabilities(outs)
    signals = []

    for name, p in fair.items():
        best = best_price_filtered(outs, name, p)
        if not best:
            continue
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        val   = smooth_validation(prob, odds, counts.get(name, 0), ev, edge)
        color, label, reason, stake = classify_soccer_value(prob, val, ev, edge, odds)

        # Solo incluir si tiene value real
        if color == "red":
            continue

        signals.append(enrich({
            "mode":         "value",
            "sport":        sport,
            "market":       "Moneyline",
            "short_market": "ML",
            "selection":    name,
            "probability":  prob,
            "validation":   val,
            "color":        color,
            "label":        label,
            "reason":       reason,
            "stake":        stake,
            "ev":           ev,
            "edge":         edge,
            "odds":         odds,
            "decimal_odds": best["decimal_odds"],
            "bookmaker":    best["bookmaker"],
            "book_count":   counts.get(name, 0),
            "is_primary":   True,
        }))

    return sorted(signals, key=lambda x: (x.get("ev") or 0, x["odds"]), reverse=True)

# ---------------------------------------------------------------------------
# Predicción por juego
# ---------------------------------------------------------------------------
def game_prediction_efficiency(game: dict, sport: str) -> dict:
    name = f"{game.get('away_team')} vs {game.get('home_team')}"
    ml   = moneyline_efficiency(game, sport)
    primary = ml[0] if ml else None
    return {
        "sport":      sport,
        "game":       name,
        "home_team":  game.get("home_team"),
        "away_team":  game.get("away_team"),
        "start_time": game.get("commence_time"),
        "best_bet":   primary,
        "signals":    ml,
        "mode":       "efficiency",
    }

def game_prediction_soccer(game: dict, sport: str) -> dict:
    name    = f"{game.get('away_team')} vs {game.get('home_team')}"
    signals = moneyline_soccer_value(game, sport)
    return {
        "sport":      sport,
        "game":       name,
        "home_team":  game.get("home_team"),
        "away_team":  game.get("away_team"),
        "start_time": game.get("commence_time"),
        "signals":    signals,
        "best_bet":   signals[0] if signals else None,
        "mode":       "value",
    }

# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------
def dedupe(items: list) -> list:
    seen: set = set()
    out = []
    for x in items:
        k = (x.get("game"), x.get("selection"), x.get("short_market"))
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out

def ticket_ok_efficiency(sig: Optional[dict]) -> bool:
    if not sig or sig.get("color") not in ("green", "blue"):
        return False
    if sig.get("validation", 0) < 60:
        return False
    odds = sig.get("odds")
    if odds is not None and odds <= -500:
        return False
    return True

def ticket_ok_value(sig: Optional[dict]) -> bool:
    if not sig or sig.get("color") not in ("gold", "silver"):
        return False
    ev = sig.get("ev") or 0
    return ev >= VALUE_MIN_EV

def build_ticket(name: str, pool: list, count: int, ok_fn, color: str, risk: str, reason: str) -> Optional[dict]:
    picks = []
    games_seen: set = set()
    for s in pool:
        if len(picks) >= count:
            break
        if s.get("game") in games_seen or not ok_fn(s):
            continue
        picks.append(s)
        games_seen.add(s.get("game"))

    if len(picks) < min(count, 3):
        return None

    comb = 1.0
    avg  = 0.0
    for x in picks:
        comb *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg  += x.get("validation", 0)
    avg /= len(picks)

    return {
        "name":                 name,
        "color":                color,
        "picks":                picks,
        "validation":           round(clamp(avg, *VAL_CLAMP), 1),
        "combined_probability": round(clamp(comb * 100, 0.1, 95), 1),
        "risk":                 risk,
        "reason":               reason,
    }

# ---------------------------------------------------------------------------
# Dashboard principal v8.3
# ---------------------------------------------------------------------------
def get_dashboard(selected_sports: list, force_refresh: bool = False) -> dict:
    dash: dict = {
        "mode":             "Pro v8.3 - Efficiency + Soccer Value",
        "cache_ttl_seconds": CACHE_TTL,
        "only_today":       ONLY_TODAY,
        "timezone":         LOCAL_TZ,
        "sports":           {},
        "games":            [],

        # Eficiencia (MLB/NBA/NHL) - difícil perder
        "efficiency_green": [],   # 🔒 Sólidos
        "efficiency_blue":  [],   # 📌 Probables

        # Soccer - value/odds altos
        "value_gold":   [],       # 💎 Value Alto
        "value_silver": [],       # 🎯 Value

        # Tickets
        "ticket_efficiency": None,
        "ticket_value":      None,

        "warnings": [],
    }

    # --- Deportes de eficiencia ---
    for label, key in SPORTS_EFFICIENCY.items():
        if label not in selected_sports and "ALL" not in selected_sports:
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
                pred = game_prediction_efficiency(g, label)
                dash["games"].append(pred)
                bb = pred.get("best_bet")
                if bb:
                    entry = {**bb, "game": pred["game"], "start_time": pred["start_time"]}
                    if bb.get("color") == "green":
                        dash["efficiency_green"].append(entry)
                    elif bb.get("color") == "blue":
                        dash["efficiency_blue"].append(entry)
        except Exception as e:
            logger.error("Error %s: %s", label, e, exc_info=True)
            dash["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
            dash["warnings"].append(f"{label}: {e}")

    # --- Soccer value ---
    for label, key in SPORTS_SOCCER.items():
        try:
            games, from_cache, ttl = fetch_odds(key, force_refresh=force_refresh)
            total = len(games)
            games = [g for g in games if is_game_today(g)]
            if not games:
                continue
            dash["sports"][label] = {
                "ok": True, "sport_key": key,
                "games_count": len(games), "total_api_games": total,
                "from_cache": from_cache, "cache_seconds_left": ttl,
            }
            for g in games:
                pred = game_prediction_soccer(g, label)
                dash["games"].append(pred)
                for sig in pred.get("signals", []):
                    entry = {**sig, "game": pred["game"], "start_time": pred["start_time"]}
                    if sig.get("color") == "gold":
                        dash["value_gold"].append(entry)
                    elif sig.get("color") == "silver":
                        dash["value_silver"].append(entry)
        except Exception as e:
            logger.warning("Soccer %s no disponible: %s", label, e)
            dash["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}

    # Ordenar y limitar
    sorter_eff = lambda x: (x.get("validation", 0), x.get("probability", 0))
    sorter_val = lambda x: (x.get("ev") or 0, x.get("odds", 0))

    dash["efficiency_green"] = dedupe(sorted(dash["efficiency_green"], key=sorter_eff, reverse=True))[:10]
    dash["efficiency_blue"]  = dedupe(sorted(dash["efficiency_blue"],  key=sorter_eff, reverse=True))[:10]
    dash["value_gold"]       = dedupe(sorted(dash["value_gold"],   key=sorter_val, reverse=True))[:15]
    dash["value_silver"]     = dedupe(sorted(dash["value_silver"], key=sorter_val, reverse=True))[:10]

    # Tickets
    eff_pool = dedupe(dash["efficiency_green"] + dash["efficiency_blue"])
    eff_pool.sort(key=sorter_eff, reverse=True)
    dash["ticket_efficiency"] = build_ticket(
        "🔒 Ticket Eficiencia", eff_pool, 4, ticket_ok_efficiency,
        "green", "Bajo", "Picks de alta probabilidad en MLB/NBA/NHL."
    )

    val_pool = dedupe(dash["value_gold"] + dash["value_silver"])
    val_pool.sort(key=sorter_val, reverse=True)
    dash["ticket_value"] = build_ticket(
        "💎 Ticket Value Soccer", val_pool, 4, ticket_ok_value,
        "gold", "Medio-Alto", "Soccer con odds altos y valor matemático positivo."
    )

    # Compatibilidad con frontend v8.2
    dash["green"]      = dash["efficiency_green"]
    dash["blue"]       = dash["efficiency_blue"]
    dash["red"]        = []
    dash["top_profit"] = dash["value_gold"][:3]
    dash["tickets"]    = [t for t in [dash["ticket_efficiency"], dash["ticket_value"]] if t]

    return dash

# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def debug_all_sports() -> dict:
    res = {
        "api_key": api_key_status(),
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
            }
        except Exception as e:
            res["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
    return res
