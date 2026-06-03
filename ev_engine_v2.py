"""
ev_engine.py — EV Engine mejorado
Cambios principales:
  - Caché thread-safe con threading.Lock
  - Logging estructurado en lugar de strings crudos
  - smooth_validation descompuesta y más transparente
  - best_price_filtered con advertencia explícita cuando usa fallback
  - alt_market_signals con corrección de vig básica
  - build_ticket sin magic strings
  - Constantes centralizadas
  - Anotaciones de tipo
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ev_engine")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY: str = os.getenv("ODDS_API_KEY", "")
SPORTS: dict[str, str] = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
}
REGIONS: str = os.getenv("REGIONS", "us")
MARKETS: str = os.getenv("MARKETS", "h2h,spreads,totals")
ODDS_FORMAT: str = "american"
LOCAL_TZ: str = os.getenv("LOCAL_TZ", "America/New_York")
ONLY_TODAY: bool = os.getenv("ONLY_TODAY", "1") == "1"
CACHE_TTL: int = int(os.getenv("CACHE_TTL", "900"))

# Límites globales de validación/EV (centralizados para fácil ajuste)
EV_CLAMP = (-25.0, 25.0)
EDGE_CLAMP = (-20.0, 20.0)
PROB_CLAMP = (1.0, 85.0)
VAL_CLAMP = (1.0, 95.0)

# ---------------------------------------------------------------------------
# Caché thread-safe
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_get(key: str) -> Optional[tuple[list, int]]:
    """Devuelve (data, segundos_restantes) si el entry es válido, else None."""
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
# API
# ---------------------------------------------------------------------------
def api_key_status() -> str:
    if not API_KEY:
        return "missing"
    if len(API_KEY) < 10:
        return "too_short"
    return "present"


def fetch_odds(sport_key: str, force_refresh: bool = False) -> tuple[list, bool, int]:
    """
    Retorna (data, from_cache, segundos_restantes).
    Lanza RuntimeError si la API falla.
    """
    if not API_KEY or API_KEY == "pon_tu_api_key_aqui":
        raise RuntimeError("Falta ODDS_API_KEY en Render Environment")

    cache_key = f"{sport_key}:{REGIONS}:{MARKETS}:{ODDS_FORMAT}"

    if not force_refresh:
        cached = _cache_get(cache_key)
        if cached:
            data, remaining = cached
            logger.debug("Cache hit para %s (%ds restantes)", sport_key, remaining)
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
# Filtros de juegos
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
def get_market_outcomes(game: dict, mkey: str) -> list[dict]:
    rows = []
    for b in game.get("bookmakers", []):
        title = b.get("title", "Unknown")
        for m in b.get("markets", []):
            if m.get("key") != mkey:
                continue
            for o in m.get("outcomes", []):
                rows.append(
                    {
                        "bookmaker": title,
                        "market": mkey,
                        "name": o.get("name"),
                        "price": o.get("price"),
                        "point": o.get("point"),
                    }
                )
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
def no_vig_h2h_probabilities(outcomes: list) -> tuple[dict, dict]:
    """
    Calcula probabilidades sin vig promediando todos los libros.
    Retorna (probs_dict, book_count_dict).
    """
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
        n: clamp(v / counts.get(n, 1), 0.01, 0.99) for n, v in probs.items()
    }
    return result, counts


# ---------------------------------------------------------------------------
# Selección de mejor precio con filtro de sanity
# ---------------------------------------------------------------------------
def best_price_filtered(
    outcomes: list, name: str, fair: float, point=None
) -> Optional[dict]:
    """
    Busca el mejor precio para `name` que no diverja más de 25% de `fair`.
    Si no hay candidatos sanos, usa el menos divergente (con advertencia en log).
    """
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
        cand.append(
            {
                "bookmaker": o.get("bookmaker", "Unknown"),
                "american_odds": int(price),
                "decimal_odds": dec,
                "point": o.get("point"),
                "diff": abs(imp - fair),
            }
        )
    if not cand:
        return None

    sane = [c for c in cand if c["diff"] <= 0.25]
    if not sane:
        fallback = sorted(cand, key=lambda c: c["diff"])[0]
        logger.warning(
            "best_price_filtered: ningún precio sano para '%s' (fair=%.2f). "
            "Usando fallback: %s @ %+d (diff=%.2f)",
            name, fair, fallback["bookmaker"], fallback["american_odds"], fallback["diff"],
        )
        return fallback

    return sorted(sane, key=lambda c: c["decimal_odds"], reverse=True)[0]


# ---------------------------------------------------------------------------
# EV, edge y validación
# ---------------------------------------------------------------------------
def safe_ev_edge(fair: float, odds: int) -> tuple[Optional[float], Optional[float]]:
    dec = american_to_decimal(odds)
    imp = implied_probability_american(odds)
    if dec is None or imp is None:
        return None, None
    ev = round(clamp(calculate_ev(fair, dec) * 100, *EV_CLAMP), 1)
    edge = round(clamp((fair - imp) * 100, *EDGE_CLAMP), 1)
    return ev, edge


def _odds_adjustment(odds: Optional[int]) -> float:
    """Ajuste a la validación basado solo en las odds (aislado para testear)."""
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


def smooth_validation(
    prob: float,
    odds: Optional[int],
    books: int,
    ev: float = 0.0,
    edge: float = 0.0,
) -> float:
    """
    Ajusta la probabilidad base para obtener una puntuación de validación.
    Desglosado en componentes para facilitar testing y comprensión.
    """
    prob = clamp(prob, 1, 85)
    ev = clamp(ev or 0.0, -10, 10)
    edge = clamp(edge or 0.0, -8, 8)

    adj_books = min((books or 0) * 0.6, 5.0)
    adj_odds = _odds_adjustment(odds)
    adj_ev = clamp(ev * 0.35, -3, 4)
    adj_edge = clamp(edge * 0.45, -3, 4)

    total_adj = adj_books + adj_odds + adj_ev + adj_edge
    return round(clamp(prob + total_adj, *VAL_CLAMP), 1)


# ---------------------------------------------------------------------------
# Clasificación y enriquecimiento
# ---------------------------------------------------------------------------
def classify_pick(
    prob: float, val: float, ev: float, edge: float, odds: Optional[int]
) -> tuple[str, str, str, str]:
    ev = ev if ev is not None else 0.0
    edge = edge if edge is not None else 0.0

    if odds is not None and odds <= -400 and val >= 52 and prob >= 48:
        return "blue", "PROBABLE", "Probable ganador, pero cuota demasiado cara.", "0u-0.25u"
    if val >= 68 and prob >= 58 and ev > -3 and edge > -3:
        return "green", "SEGURIDAD A JUGAR", "Alta validación con probabilidad sólida.", "0.50u-1u"
    if val >= 52 and prob >= 48:
        return "blue", "PROBABLE", "Probable ganador o mercado aceptable.", "0u-0.25u"
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
        return "🔒 Favorito sólido"
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
def moneyline_signals(game: dict) -> list[dict]:
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
        signals.append(
            enrich(
                {
                    "market": "Moneyline",
                    "short_market": "ML",
                    "selection": name,
                    "probability": prob,
                    "validation": val,
                    "color": color,
                    "label": label,
                    "reason": reason,
                    "stake": stake,
                    "ev": ev,
                    "edge": edge,
                    "odds": odds,
                    "point": None,
                    "bookmaker": best["bookmaker"],
                    "book_count": counts.get(name, 0),
                    "is_primary": True,
                }
            )
        )
    return sorted(signals, key=lambda x: (x["validation"], x["probability"]), reverse=True)


def alt_market_signals(game: dict) -> list[dict]:
    """
    Señales para spreads y totals.
    MEJORA: aplica corrección de vig básica (normaliza las dos piernas)
    en lugar de usar implied probability cruda como validación.
    """
    signals = []
    for key, short in [("spreads", "Spread"), ("totals", "Total")]:
        outs = get_market_outcomes(game, key)

        # Agrupar por (name, point) para calcular vig por par
        pairs: dict[tuple, list] = {}
        for o in outs:
            k = (o.get("name"), o.get("point"))
            if None not in k:
                pairs.setdefault(k, []).append(o)

        seen: set = set()
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

            # Corrección de vig: buscar la otra pierna del mismo punto
            opposite_name = "Over" if name == "Under" else ("Under" if name == "Over" else None)
            if opposite_name:
                opp_best = best_price_same(outs, opposite_name, point)
                if opp_best:
                    opp_imp = implied_probability_american(opp_best["american_odds"])
                    if opp_imp:
                        total_imp = imp + opp_imp
                        if total_imp > 0:
                            imp = imp / total_imp  # probabilidad sin vig

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
            reason = (
                "Mercado alternativo con precio razonable."
                if val >= 58
                else "No tiene suficiente validación como segunda jugada."
            )
            title = (
                f"{name} {point}" if key == "totals" else f"{name} {point:+g}"
            )
            signals.append(
                enrich(
                    {
                        "market": short,
                        "short_market": short,
                        "selection": title,
                        "probability": prob,
                        "validation": val,
                        "color": color,
                        "label": label,
                        "reason": reason,
                        "stake": "0u-0.25u" if val >= 58 else "0u",
                        "ev": None,
                        "edge": None,
                        "odds": odds_val,
                        "point": point,
                        "bookmaker": best["bookmaker"],
                        "is_primary": False,
                    }
                )
            )
    return sorted(signals, key=lambda x: x["validation"], reverse=True)


# ---------------------------------------------------------------------------
# Predicciones y tickets
# ---------------------------------------------------------------------------
def prediction_summary(ml: list) -> Optional[dict]:
    if not ml:
        return None
    w = sorted(ml, key=lambda x: x["probability"], reverse=True)[0]
    return {
        "selection": w["selection"],
        "probability": w["probability"],
        "odds": w["odds"],
        "bookmaker": w["bookmaker"],
        "note": "Pronóstico ML separado: indica quién tiene más probabilidad de ganar, no necesariamente que conviene apostarlo.",
    }


def best_bet_for_game(
    primary: Optional[dict], secondary: Optional[dict]
) -> Optional[dict]:
    candidates = [x for x in [secondary, primary] if x]
    playable = [x for x in candidates if x.get("color") in ("green", "blue")]
    if playable:
        return sorted(
            playable,
            key=lambda x: (x.get("validation", 0), x.get("probability", 0)),
            reverse=True,
        )[0]
    return secondary or primary


def game_prediction(game: dict, sport: str) -> dict:
    name = f"{game.get('away_team')} vs {game.get('home_team')}"
    ml = moneyline_signals(game)
    alt = alt_market_signals(game)
    primary = ml[0] if ml else None
    secondary = next((s for s in alt if s["color"] != "red"), alt[0] if alt else None)
    best = best_bet_for_game(primary, secondary)
    return {
        "sport": sport,
        "game": name,
        "home_team": game.get("home_team"),
        "away_team": game.get("away_team"),
        "start_time": game.get("commence_time"),
        "ml_prediction": prediction_summary(ml),
        "primary_pick": primary,
        "secondary_pick": secondary,
        "best_bet": best,
        "signals": [x for x in [primary, secondary] if x],
        "moneyline_options": ml,
        "alt_options": alt,
        "note": "v8.2: pronóstico ganador separado de recomendación de apuesta.",
    }


def dedupe(items: list) -> list:
    seen: set = set()
    out = []
    for x in items:
        k = (x.get("game"), x.get("selection"), x.get("short_market"))
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def ticket_ok(sig: Optional[dict]) -> bool:
    if not sig or sig.get("color") == "red" or sig.get("validation", 0) < 58:
        return False
    odds = sig.get("odds")
    if odds is not None and (odds <= -450 or odds >= 700):
        return False
    return True


# Configuración de tickets sin magic strings
TICKET_CONFIGS = [
    {"name": "Ticket 3", "count": 3, "color": "green", "risk": "Medio",
     "reason": "Ticket principal: solo picks con mejor validación.",
     "min_picks": 3, "ticket_type": "Profit Ticket"},
    {"name": "Ticket 6", "count": 6, "color": "blue", "risk": "Alto",
     "reason": "Solo se genera si hay 6 señales fuertes.",
     "min_picks": 6, "ticket_type": "Profit Ticket"},
    {"name": "Ticket 10", "count": 10, "color": "red", "risk": "Extremo",
     "reason": "Lottery Ticket: alto retorno, usar stake mínimo.",
     "min_picks": 6, "ticket_type": "Lottery Ticket"},
]


def build_ticket(cfg: dict, pool: list) -> Optional[dict]:
    picks = []
    games_seen: set = set()
    for s in pool:
        if len(picks) >= cfg["count"]:
            break
        if s.get("game") in games_seen or not ticket_ok(s):
            continue
        picks.append(s)
        games_seen.add(s.get("game"))

    if len(picks) < cfg["min_picks"]:
        return None

    comb = 1.0
    avg = 0.0
    for x in picks:
        comb *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg += x.get("validation", 0)
    avg /= len(picks)

    return {
        "name": cfg["name"],
        "color": cfg["color"],
        "target_count": cfg["count"],
        "picks": picks,
        "validation": round(clamp(avg, *VAL_CLAMP), 1),
        "combined_probability": round(clamp(comb * 100, 0.1, 95), 1),
        "risk": cfg["risk"],
        "reason": cfg["reason"],
        "ticket_type": cfg["ticket_type"],
    }


def build_tickets(green: list, blue: list) -> list:
    pool = dedupe(green + blue)
    pool.sort(key=lambda x: (x.get("validation", 0), x.get("confidence_score", 0)), reverse=True)
    tickets = [build_ticket(cfg, pool) for cfg in TICKET_CONFIGS]
    return [t for t in tickets if t]


# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------
def get_dashboard(selected_sports: list, force_refresh: bool = False) -> dict:
    dash: dict = {
        "mode": "Pro v8.2 Improved",
        "credit_saving": True,
        "cache_ttl_seconds": CACHE_TTL,
        "only_today": ONLY_TODAY,
        "timezone": LOCAL_TZ,
        "sports": {},
        "games": [],
        "top_profit": [],
        "green": [],
        "blue": [],
        "red": [],
        "tickets": [],
        "warnings": [],
    }

    for label in selected_sports:
        key = SPORTS.get(label)
        if not key:
            continue
        try:
            games, from_cache, ttl = fetch_odds(key, force_refresh=force_refresh)
            total = len(games)
            games = [g for g in games if is_game_today(g)]
            dash["sports"][label] = {
                "ok": True,
                "sport_key": key,
                "games_count": len(games),
                "total_api_games": total,
                "today_only": ONLY_TODAY,
                "timezone": LOCAL_TZ,
                "from_cache": from_cache,
                "cache_seconds_left": ttl,
            }
            for g in games:
                pred = game_prediction(g, label)
                dash["games"].append(pred)
                bb = pred.get("best_bet")
                if bb and bb.get("color") in ("green", "blue"):
                    p = bb.copy()
                    p.update({"sport": label, "game": pred["game"], "start_time": pred["start_time"]})
                    dash[p["color"]].append(p)
                elif pred.get("primary_pick"):
                    p = pred["primary_pick"].copy()
                    p.update({"sport": label, "game": pred["game"], "start_time": pred["start_time"]})
                    dash["red"].append(p)
        except Exception as e:
            logger.error("Error procesando %s: %s", label, e, exc_info=True)
            dash["sports"][label] = {"ok": False, "sport_key": key, "games_count": 0, "error": str(e)}
            dash["warnings"].append(f"{label}: {e}")

    sorter = lambda x: (x.get("validation", 0), x.get("confidence_score", 0), x.get("probability", 0))
    dash["green"] = dedupe(sorted(dash["green"], key=sorter, reverse=True))[:10]
    dash["blue"] = dedupe(sorted(dash["blue"], key=sorter, reverse=True))[:14]
    dash["red"] = dedupe(sorted(dash["red"], key=sorter, reverse=True))[:15]
    dash["top_profit"] = dedupe(dash["green"] + dash["blue"])[:3]
    dash["tickets"] = build_tickets(dash["green"], dash["blue"])
    return dash


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def debug_all_sports() -> dict:
    res = {
        "api_key": api_key_status(),
        "regions": REGIONS,
        "markets": MARKETS,
        "odds_format": ODDS_FORMAT,
        "cache_ttl_seconds": CACHE_TTL,
        "only_today": ONLY_TODAY,
        "timezone": LOCAL_TZ,
        "sports": {},
    }
    for label, key in SPORTS.items():
        try:
            games, fc, ttl = fetch_odds(key)
            total = len(games)
            games = [g for g in games if is_game_today(g)]
            res["sports"][label] = {
                "ok": True,
                "sport_key": key,
                "games_count": len(games),
                "total_api_games": total,
                "today_only": ONLY_TODAY,
                "timezone": LOCAL_TZ,
                "from_cache": fc,
                "cache_seconds_left": ttl,
                "sample_games": [
                    {
                        "home_team": g.get("home_team"),
                        "away_team": g.get("away_team"),
                        "commence_time": g.get("commence_time"),
                        "bookmakers_count": len(g.get("bookmakers", [])),
                    }
                    for g in games[:5]
                ],
            }
        except Exception as e:
            logger.error("debug_all_sports error en %s: %s", label, e)
            res["sports"][label] = {"ok": False, "sport_key": key, "games_count": 0, "error": str(e)}
    return res
