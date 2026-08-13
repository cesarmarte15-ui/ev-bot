"""
ev_engine_v8.5.py
- MLB/NBA/NHL: modo EFICIENCIA - favoritos sólidos, difícil perder
- v8.5: fallback ML→Spread/Total, umbrales más flexibles
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

SPORTS_EFFICIENCY: dict[str, str] = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
}

SPORTS: dict[str, str] = SPORTS_EFFICIENCY

REGIONS: str     = os.getenv("REGIONS", "us")
MARKETS: str     = os.getenv("MARKETS", "h2h,spreads,totals")
ODDS_FORMAT: str = "american"
LOCAL_TZ: str    = os.getenv("LOCAL_TZ", "America/New_York")
ONLY_TODAY: bool = os.getenv("ONLY_TODAY", "1") == "1"
CACHE_TTL: int   = int(os.getenv("CACHE_TTL", "900"))

# Umbrales
EFFICIENCY_MIN_PROB     = 60.0   # % mínimo para pick eficiente
EFFICIENCY_MIN_VAL      = 63.0   # validación mínima
EFFICIENCY_MAX_ODDS     = -130   # no más caro que -130 en americano para eficiencia
EFFICIENCY_MIN_EV_GREEN = 0.0    # piso de EV para SÓLIDO
EFFICIENCY_MIN_EV_BLUE  = -1.0   # piso de EV para PROBABLE

MAX_ODDS_DIFF_PCT = float(os.getenv("MAX_ODDS_DIFF_PCT", "0.06"))

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
    """
    Agrega por (name, point), no solo por name. En spreads/totals cada libro
    puede publicar una línea distinta (-3.5 en uno, -2.5 en otro); agregar
    solo por name promediaba probabilidades de líneas distintas como si
    fueran el mismo mercado. Para h2h (moneyline) point es siempre None,
    así que el comportamiento no cambia ahí.
    """
    by_book: dict[str, list] = {}
    for o in outcomes:
        name = o.get("name")
        point = o.get("point")
        price = o.get("price")
        book = o.get("bookmaker", "Unknown")
        if name is None or price is None:
            continue
        p = implied_probability_american(price)
        if p is not None:
            by_book.setdefault(book, []).append((name, point, p))

    probs: dict[tuple, float] = {}
    counts: dict[tuple, int] = {}
    for items in by_book.values():
        if len(items) < 2:
            continue
        total = sum(p for _, _, p in items)
        if total <= 0:
            continue
        for name, point, p in items:
            fair = p / total
            key = (name, point)
            probs[key] = probs.get(key, 0) + fair
            counts[key] = counts.get(key, 0) + 1

    result = {
        k: clamp(v / counts.get(k, 1), 0.01, 0.99)
        for k, v in probs.items()
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
    sane = [c for c in cand if c["diff"] <= MAX_ODDS_DIFF_PCT]
    if not sane:
        fallback = sorted(cand, key=lambda c: c["diff"])[0]
        return fallback
    return sorted(sane, key=lambda c: c["decimal_odds"], reverse=True)[0]

def fanduel_price(outcomes: list, name: str, point=None) -> Optional[int]:
    for o in outcomes:
        if o.get("bookmaker") != "FanDuel":
            continue
        if o.get("name") != name:
            continue
        if point is not None and o.get("point") != point:
            continue
        price = o.get("price")
        if price is not None:
            return int(price)
    return None

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
    if odds <= -250:  return -2.0
    if odds <= -150:  return 2.0
    if odds >= 600:   return -8.0
    if odds >= 300:   return -4.0
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
def efficiency_failure_reason(prob, val, ev, odds, odds_ok) -> str:
    """
    Detalla contra qué umbral(es) de PROBABLE (el piso más bajo para no caer
    en EVITAR) falló el pick, para diagnosticar sin tener que inspeccionar
    prob/val/ev manualmente en cada caso.
    """
    fails = []
    if prob < 58:
        fails.append(f"Prob {prob:.1f}% <58%")
    if val < 60:
        fails.append(f"Val {val:.1f} <60")
    if ev < EFFICIENCY_MIN_EV_BLUE:
        fails.append(f"EV {ev:.1f}% <{EFFICIENCY_MIN_EV_BLUE:.1f}%")
    if not odds_ok:
        fails.append(f"Odds {odds} peor que {EFFICIENCY_MAX_ODDS}")
    if not fails:
        return "No cumple criterios de eficiencia."
    return "Falla: " + " · ".join(fails)

def classify_efficiency(prob, val, ev, edge, odds) -> tuple[str, str, str, str]:
    ev    = ev or 0.0
    edge  = edge or 0.0
    odds_ok = odds is None or odds >= EFFICIENCY_MAX_ODDS

    if (prob >= EFFICIENCY_MIN_PROB and val >= EFFICIENCY_MIN_VAL and ev >= EFFICIENCY_MIN_EV_GREEN
            and edge >= -0.5 and odds_ok):
        return "green", "🔒 SÓLIDO", "Favorito con alta probabilidad y buen valor.", "0.5u-1u"

    if prob >= 58 and val >= 60 and ev >= EFFICIENCY_MIN_EV_BLUE and odds_ok:
        return "blue", "📌 PROBABLE", "Alta probabilidad pero precio ajustado.", "0.25u"

    reason = efficiency_failure_reason(prob, val, ev, odds, odds_ok)
    return "red", "⚠ EVITAR", reason, "0u"

# ---------------------------------------------------------------------------
# Enriquecimiento
# ---------------------------------------------------------------------------
def confidence_score(v) -> float:
    return round(clamp((v or 0) / 10, 0.1, 9.9), 1)

def enrich(sig: dict) -> dict:
    sig["confidence_score"] = confidence_score(sig.get("validation"))
    sig["is_bet_recommendation"] = sig.get("color") in ("green", "blue")
    return sig

# ---------------------------------------------------------------------------
# Señales ML - EFICIENCIA
# ---------------------------------------------------------------------------
def moneyline_efficiency(game: dict, sport: str) -> list[dict]:
    outs = get_market_outcomes(game, "h2h")
    fair, counts = no_vig_h2h_probabilities(outs)
    signals = []

    for (name, _point), p in fair.items():
        best = best_price_filtered(outs, name, p)
        if not best:
            continue
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        val   = smooth_validation(prob, odds, counts.get((name, _point), 0), ev, edge)
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds)

        fd_odds = fanduel_price(outs, name)
        signals.append(enrich({
            "mode":              "efficiency",
            "sport":             sport,
            "market":            "Moneyline",
            "short_market":      "ML",
            "selection":         name,
            "probability":       prob,
            "validation":        val,
            "color":             color,
            "label":             label,
            "reason":            reason,
            "stake":             stake,
            "ev":                ev,
            "edge":              edge,
            "odds":              odds,
            "decimal_odds":      best["decimal_odds"],
            "bookmaker":         best["bookmaker"],
            "book_count":        counts.get((name, _point), 0),
            "is_primary":        True,
            "fanduel_odds":      fd_odds,
            "fanduel_available": fd_odds is not None,
        }))

    return sorted(signals, key=lambda x: (x["validation"], x["probability"]), reverse=True)

# ---------------------------------------------------------------------------
# Señales Spread y Total - EFICIENCIA
# ---------------------------------------------------------------------------
def spread_signals(game: dict, sport: str) -> list[dict]:
    outs = get_market_outcomes(game, "spreads")
    if not outs:
        return []
    fair, counts = no_vig_h2h_probabilities(outs)
    signals = []
    for (name, point), p in fair.items():
        best = best_price_filtered(outs, name, p, point=point)
        if not best:
            continue
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        val   = smooth_validation(prob, odds, counts.get((name, point), 0), ev, edge)
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds)
        fd_odds   = fanduel_price(outs, name, point)
        point_str = (f"+{point}" if (point or 0) > 0 else str(point)) if point is not None else ""
        signals.append(enrich({
            "mode":              "efficiency",
            "sport":             sport,
            "market":            "Spread",
            "short_market":      "SPR",
            "selection":         f"{name} {point_str}".strip(),
            "probability":       prob,
            "validation":        val,
            "color":             color,
            "label":             label,
            "reason":            reason,
            "stake":             stake,
            "ev":                ev,
            "edge":              edge,
            "odds":              odds,
            "decimal_odds":      best["decimal_odds"],
            "bookmaker":         best["bookmaker"],
            "book_count":        counts.get((name, point), 0),
            "is_primary":        False,
            "fanduel_odds":      fd_odds,
            "fanduel_available": fd_odds is not None,
            "point":             point,
        }))
    return sorted(signals, key=lambda x: (x.get("ev") or 0, x["validation"], x["probability"]), reverse=True)


def total_signals(game: dict, sport: str) -> list[dict]:
    outs = get_market_outcomes(game, "totals")
    if not outs:
        return []
    fair, counts = no_vig_h2h_probabilities(outs)
    signals = []
    for (name, point), p in fair.items():
        best = best_price_filtered(outs, name, p, point=point)
        if not best:
            continue
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        val   = smooth_validation(prob, odds, counts.get((name, point), 0), ev, edge)
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds)
        fd_odds   = fanduel_price(outs, name, point)
        point_str = str(point) if point is not None else ""
        signals.append(enrich({
            "mode":              "efficiency",
            "sport":             sport,
            "market":            "Total",
            "short_market":      "TOT",
            "selection":         f"{name} {point_str}".strip(),
            "probability":       prob,
            "validation":        val,
            "color":             color,
            "label":             label,
            "reason":            reason,
            "stake":             stake,
            "ev":                ev,
            "edge":              edge,
            "odds":              odds,
            "decimal_odds":      best["decimal_odds"],
            "bookmaker":         best["bookmaker"],
            "book_count":        counts.get((name, point), 0),
            "is_primary":        False,
            "fanduel_odds":      fd_odds,
            "fanduel_available": fd_odds is not None,
            "point":             point,
        }))
    return sorted(signals, key=lambda x: (x.get("ev") or 0, x["validation"], x["probability"]), reverse=True)


# ---------------------------------------------------------------------------
# Predicción completa por juego (v8.5)
# ---------------------------------------------------------------------------
def best_market(ml: list, spread: list, total: list) -> Optional[dict]:
    """
    Elige el mercado con mejor EV real entre ML/Spread/Total, solo entre
    picks que ya cumplen el piso de EV de SÓLIDO/PROBABLE (color green/blue).
    Si ningún mercado del partido cumple ese piso, no hay recomendación
    (antes caía a candidatos rojos y podía recomendar EV negativo).
    """
    def best_of(signals: list) -> Optional[dict]:
        good = [s for s in (signals or []) if s.get("color") in ("green", "blue")]
        if not good:
            return None
        return max(good, key=lambda s: s.get("ev") or 0)

    candidates = [
        (n, best_of(s)) for n, s in [("Moneyline", ml), ("Spread", spread), ("Total", total)]
    ]
    candidates = [(n, s) for n, s in candidates if s]
    if not candidates:
        return None
    best_name, best_sig = max(candidates, key=lambda x: x[1].get("ev") or 0)
    return {
        **best_sig,
        "recommended_market": best_name,
        "reason": f"{best_name} — Val {best_sig.get('validation')}% / EV {best_sig.get('ev')}%",
    }


def game_prediction_full(game: dict, sport: str) -> dict:
    name   = f"{game.get('away_team')} vs {game.get('home_team')}"
    ml     = moneyline_efficiency(game, sport)
    spread = spread_signals(game, sport)
    total  = total_signals(game, sport)
    return {
        "sport":       sport,
        "game":        name,
        "home_team":   game.get("home_team"),
        "away_team":   game.get("away_team"),
        "start_time":  game.get("commence_time"),
        "ml":          ml,
        "spread":      spread,
        "total":       total,
        "best_market": best_market(ml, spread, total),
        "mode":        "full",
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

def ticket_ok_mega(sig: Optional[dict]) -> bool:
    if not sig or sig.get("color") not in ("green", "blue"):
        return False
    return sig.get("probability", 0) >= 60.0 and sig.get("validation", 0) >= 58

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

    real_count = len(picks)
    if real_count < min(count, 3):
        return None

    dynamic_name = name.replace(str(count), str(real_count)) if str(count) in name else f"{name} ({real_count})"

    comb = 1.0
    avg  = 0.0
    for x in picks:
        comb *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg  += x.get("validation", 0)
    avg /= real_count

    return {
        "name":                 dynamic_name,
        "color":                color,
        "picks":                picks,
        "picks_count":          real_count,
        "target_count":         count,
        "validation":           round(clamp(avg, *VAL_CLAMP), 1),
        "combined_probability": round(clamp(comb * 100, 0.1, 95), 1),
        "risk":                 risk,
        "reason":               reason,
    }

# ---------------------------------------------------------------------------
# Dashboard principal v8.4
# ---------------------------------------------------------------------------
def get_dashboard(selected_sports: list, force_refresh: bool = False) -> dict:
    dash: dict = {
        "mode":             "Pro v8.5 - Full Market Analysis",
        "cache_ttl_seconds": CACHE_TTL,
        "only_today":       ONLY_TODAY,
        "timezone":         LOCAL_TZ,
        "sports":           {},
        "games":            [],
        "all_games":        [],

        # Eficiencia (MLB/NBA/NHL)
        "efficiency_green": [],
        "efficiency_blue":  [],

        # Picks a evitar
        "avoid": [],

        # Tickets
        "ticket_efficiency": None,
        "parlay_no_solido":  False,
        "parlay_mltotal_no_solido": False,

        "warnings": [],
    }

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
                full = game_prediction_full(g, label)
                dash["all_games"].append(full)
                # Fallback: si ML no es green/blue, buscar mejor jugada en Spread o Total
                bb = full["ml"][0] if full["ml"] else None
                bb_spread = full["spread"][0] if full["spread"] else None
                bb_total  = full["total"][0]  if full["total"]  else None

                if not bb or bb.get("color") == "red":
                    alternatives = [s for s in [bb_spread, bb_total] if s and s.get("color") in ("green", "blue")]
                    if alternatives:
                        best_alt = sorted(alternatives, key=lambda x: (x.get("validation", 0), x.get("ev") or 0), reverse=True)[0]
                        best_alt["fallback_from_ml"] = True
                        best_alt["label"] = best_alt.get("label", "") + " (Alt)"
                        bb = best_alt
                    elif bb and bb.get("color") == "red":
                        if bb.get("probability", 0) >= 55:
                            bb = {**bb, "color": "yellow", "label": "🔶 ESPECULATIVO",
                                  "reason": "ML sin valor claro — apuesta pequeña si acaso.", "stake": "0.1u"}
                        else:
                            bb = None
                pred = {
                    "sport": label, "game": full["game"],
                    "home_team": full["home_team"], "away_team": full["away_team"],
                    "start_time": full["start_time"],
                    "best_bet": bb, "signals": full["ml"], "mode": "efficiency",
                }
                dash["games"].append(pred)
                if bb:
                    entry = {**bb, "game": full["game"], "start_time": full["start_time"]}
                    if bb.get("color") == "green":
                        dash["efficiency_green"].append(entry)
                    elif bb.get("color") == "blue":
                        dash["efficiency_blue"].append(entry)
                for mkey in ("ml", "spread", "total"):
                    for sig in full.get(mkey, []):
                        if sig.get("color") == "red":
                            dash["avoid"].append({**sig, "game": full["game"], "start_time": full["start_time"]})
        except Exception as e:
            logger.error("Error %s: %s", label, e, exc_info=True)
            dash["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
            dash["warnings"].append(f"{label}: {e}")

    # Ordenar y limitar
    sorter_eff = lambda x: (x.get("validation", 0), x.get("probability", 0))

    dash["efficiency_green"] = dedupe(sorted(dash["efficiency_green"], key=sorter_eff, reverse=True))[:10]
    dash["efficiency_blue"]  = dedupe(sorted(dash["efficiency_blue"],  key=sorter_eff, reverse=True))[:10]

    # Ticket Eficiencia
    eff_pool = dedupe(dash["efficiency_green"] + dash["efficiency_blue"])
    eff_pool.sort(key=sorter_eff, reverse=True)
    dash["ticket_efficiency"] = build_ticket(
        "🔒 Ticket Eficiencia", eff_pool, 6, ticket_ok_efficiency,
        "green", "Bajo", "Picks de alta probabilidad en MLB/NBA/NHL."
    )

    # Parlay Mixto (3/6/10 legs, seleccionable) — ML + Spread + Total
    # Requiere al menos 1 pick SÓLIDO (green) para armar parlays
    _parlay_pool = []
    for _g in dash["all_games"]:
        for _mkey in ("ml", "spread", "total"):
            for _sig in _g.get(_mkey, []):
                if _sig.get("color") in ("green", "blue"):
                    _parlay_pool.append({**_sig, "game": _g["game"], "start_time": _g["start_time"]})
    _parlay_pool = dedupe(_parlay_pool)
    _parlay_pool.sort(key=lambda x: x.get("probability", 0), reverse=True)

    if any(s.get("color") == "green" for s in _parlay_pool):
        dash["ticket_3"]  = build_ticket("🎯 Parlay Mixto — 3 Legs",  _parlay_pool, 3,  ticket_ok_efficiency, "green", "Bajo",  "3 mejores picks del día (ML/Spread/Total).")
        dash["ticket_6"]  = build_ticket("🔥 Parlay Mixto — 6 Legs",  _parlay_pool, 6,  ticket_ok_efficiency, "blue",  "Medio", "6 mejores picks del día (ML/Spread/Total).")
        dash["ticket_10"] = build_ticket("⭐ Parlay Mixto — 10 Legs", _parlay_pool, 10, ticket_ok_mega,        "gold",  "Alto",  "10 mejores picks del día (ML/Spread/Total).")
    else:
        dash["ticket_3"] = dash["ticket_6"] = dash["ticket_10"] = None
        dash["parlay_no_solido"] = True

    # Parlay ML + Total (3/6/10 legs, seleccionable) — excluye Spread
    _parlay_pool_mltotal = []
    for _g in dash["all_games"]:
        for _mkey in ("ml", "total"):
            for _sig in _g.get(_mkey, []):
                if _sig.get("color") in ("green", "blue"):
                    _parlay_pool_mltotal.append({**_sig, "game": _g["game"], "start_time": _g["start_time"]})
    _parlay_pool_mltotal = dedupe(_parlay_pool_mltotal)
    _parlay_pool_mltotal.sort(key=lambda x: x.get("probability", 0), reverse=True)

    if any(s.get("color") == "green" for s in _parlay_pool_mltotal):
        dash["ticket_mltotal_3"]  = build_ticket("🎯 Parlay ML+Total — 3 Legs",  _parlay_pool_mltotal, 3,  ticket_ok_efficiency, "green", "Bajo",  "3 mejores picks del día (ML/Total).")
        dash["ticket_mltotal_6"]  = build_ticket("🔥 Parlay ML+Total — 6 Legs",  _parlay_pool_mltotal, 6,  ticket_ok_efficiency, "blue",  "Medio", "6 mejores picks del día (ML/Total).")
        dash["ticket_mltotal_10"] = build_ticket("⭐ Parlay ML+Total — 10 Legs", _parlay_pool_mltotal, 10, ticket_ok_mega,        "gold",  "Alto",  "10 mejores picks del día (ML/Total).")
    else:
        dash["ticket_mltotal_3"] = dash["ticket_mltotal_6"] = dash["ticket_mltotal_10"] = None
        dash["parlay_mltotal_no_solido"] = True

    # Deduplicar avoid
    dash["avoid"] = dedupe(dash["avoid"])

    # Picks de alta probabilidad (≥65%)
    all_eff = dedupe(dash["efficiency_green"] + dash["efficiency_blue"])
    dash["high_prob"] = sorted(
        [p for p in all_eff if p.get("probability", 0) >= 65.0],
        key=lambda x: x.get("probability", 0),
        reverse=True,
    )[:20]

    dash["green"]   = dash["efficiency_green"]
    dash["blue"]    = dash["efficiency_blue"]
    dash["red"]     = []
    dash["tickets"] = [t for t in [
        dash["ticket_efficiency"],
        dash["ticket_3"], dash["ticket_6"], dash["ticket_10"],
        dash["ticket_mltotal_3"], dash["ticket_mltotal_6"], dash["ticket_mltotal_10"],
    ] if t and t.get("picks_count", 0) >= 3]

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
