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
EFFICIENCY_MAX_ODDS     = -180   # no más caro que -180 en americano para eficiencia
EFFICIENCY_MIN_EV_GREEN = 0.0    # piso de EV para SÓLIDO
EFFICIENCY_MIN_EV_BLUE  = -1.0   # piso de EV para PROBABLE

# Piso más bajo antes de EVITAR. Usa 'edge' (puntos de probabilidad), no
# 'ev' crudo, a propósito: ev = edge * decimal_odds (ver ranking_edge), asi
# que bajar el piso de EV dejaria entrar picks de cuota alta con edge real
# bajo/nulo, justo el sesgo que ranking_edge corrige en el ranking. Exigir
# edge >= 0 mantiene la protección real (nada entra sin ventaja real medida)
# mientras baja el piso de prob/val para no vaciar Alta Prob en dias de
# partidos parejos (tipico en MLB).
EFFICIENCY_MIN_PROB_YELLOW = 55.0
EFFICIENCY_MIN_VAL_YELLOW  = 58.0

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

def is_upcoming(game: dict) -> bool:
    """
    True si commence_time todavía no pasó. The Odds API no distingue
    pregame de en vivo en la respuesta (mismo endpoint /odds para ambos) y
    una vez que el partido arranca las cuotas dejan de ser confiables para
    recomendar (casas suspenden/desactualizan líneas de forma dispareja).
    Excluimos esos partidos en vez de mostrarlos con datos potencialmente
    obsoletos.
    """
    s = game.get("commence_time")
    if not s:
        return False
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt > datetime.now(dt.tzinfo)
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

RANKING_BOOKS_FOR_FULL_CONFIDENCE = 4.0

def ranking_edge(sig: dict) -> float:
    """
    Metrica para ORDENAR/SELECCIONAR picks, en vez de 'ev' crudo.
    ev = edge * decimal_odds (identidad exacta de safe_ev_edge): con el
    mismo edge real, un '+200' muestra el triple de EV% que un '-150' solo
    por el multiplicador de pago, no porque el pick sea mejor. Rankear por
    'edge' (puntos de probabilidad) saca ese multiplicador de la ecuacion.
    Ademas se shrinkea por cantidad de casas de acuerdo (book_count): un
    edge medido con pocas casas es mas ruido que señal, y ese ruido tiende
    a concentrarse justo en las cuotas mas extremas que 'ev' ya sobrevalora.
    'ev' se sigue calculando y mostrando igual, solo deja de ser el criterio
    de orden/seleccion.
    """
    edge = sig.get("edge") or 0.0
    books = sig.get("book_count") or 0
    confidence = clamp(books / RANKING_BOOKS_FOR_FULL_CONFIDENCE, 0.0, 1.0)
    return edge * confidence

# ---------------------------------------------------------------------------
# Kelly score — criterio único para Ranking Kelly y Tickets/Parlays
# ---------------------------------------------------------------------------
KELLY_FRACTION    = 0.25   # Kelly fraccional (1/4): full Kelly apuesta demasiado agresivo
KELLY_SCORE_SCALE = 200.0  # escala kelly_usado (fraccion de bankroll) a un score 0-10
KELLY_MIN_SCORE   = 2.0    # piso para aparecer en Ranking Kelly y para admitir una pata en Tickets

def kelly_score(sig: dict) -> float:
    """
    Score único 0-10 que combina probabilidad real y cuota (Kelly
    Criterion) en un solo número, en vez de mostrar EV%/probabilidad
    sueltos. Usado tanto por la pestaña Ranking Kelly como por el criterio
    de admisión/orden de Tickets/Parlays — el mismo número en todo el
    sitio, para que ninguna pestaña de decisión contradiga a otra.

    kelly_completo = p - (1-p)/b            (b = cuota_decimal - 1)
    kelly_usado    = kelly_completo * KELLY_FRACTION * confianza_libros
    score          = clamp(kelly_usado * KELLY_SCORE_SCALE, 0, 10)

    confianza_libros reutiliza el mismo shrink que ranking_edge: un edge
    medido con pocas casas es mas ruido que señal. Constantes son punto de
    partida (mockup revisado con el usuario), no un resultado calibrado.
    """
    prob = sig.get("probability")
    decimal_odds = sig.get("decimal_odds")
    if prob is None or decimal_odds is None or decimal_odds <= 1.0:
        return 0.0
    p = clamp(prob, 0.0, 100.0) / 100.0
    b = decimal_odds - 1.0
    kelly_full = max(p - (1.0 - p) / b, 0.0)
    books = sig.get("book_count") or 0
    confidence = clamp(books / RANKING_BOOKS_FOR_FULL_CONFIDENCE, 0.0, 1.0)
    kelly_used = kelly_full * KELLY_FRACTION * confidence
    return round(clamp(kelly_used * KELLY_SCORE_SCALE, 0.0, 10.0), 1)

KELLY_TIERS = [
    (8.0, "Excelente", "1u"),
    (6.0, "Fuerte",     "0.5u"),
    (4.0, "Moderado",   "0.25u"),
    (KELLY_MIN_SCORE, "Leve", "0.1u"),
]

def kelly_tier(score: float) -> tuple[str, str]:
    """Etiqueta y stake sugerido para un kelly_score ya calculado. Por
    debajo de KELLY_MIN_SCORE no hay tier — ese pick no debería estar en
    ninguna lista filtrada por el piso (Ranking Kelly, Tickets)."""
    for floor, label, stake in KELLY_TIERS:
        if score >= floor:
            return label, stake
    return "", "0u"

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
def efficiency_failure_reason(prob, val, edge, odds, odds_ok) -> str:
    """
    Detalla contra qué umbral(es) de ESPECULATIVO (el piso más bajo antes de
    EVITAR) falló el pick, para diagnosticar sin tener que inspeccionar
    prob/val/edge manualmente en cada caso.
    """
    fails = []
    if prob < EFFICIENCY_MIN_PROB_YELLOW:
        fails.append(f"Prob {prob:.1f}% <{EFFICIENCY_MIN_PROB_YELLOW:.0f}%")
    if val < EFFICIENCY_MIN_VAL_YELLOW:
        fails.append(f"Val {val:.1f} <{EFFICIENCY_MIN_VAL_YELLOW:.0f}")
    if edge < 0:
        fails.append(f"Edge {edge:.1f} <0 (sin ventaja real, EV% puede ser positivo solo por la cuota)")
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

    # Piso más bajo antes de EVITAR: exige edge real positivo (no EV% crudo,
    # que un pick de cuota alta puede inflar sin edge real — ver comentario
    # de EFFICIENCY_MIN_PROB_YELLOW) a cambio de aceptar menos probabilidad.
    if prob >= EFFICIENCY_MIN_PROB_YELLOW and val >= EFFICIENCY_MIN_VAL_YELLOW and edge >= 0.0 and odds_ok:
        return "yellow", "🟡 ESPECULATIVO", "Probabilidad moderada con edge real positivo — mayor riesgo.", "0.1u-0.25u"

    reason = efficiency_failure_reason(prob, val, edge, odds, odds_ok)
    return "red", "⚠ EVITAR", reason, "0u"

# ---------------------------------------------------------------------------
# Enriquecimiento
# ---------------------------------------------------------------------------
def confidence_score(v) -> float:
    return round(clamp((v or 0) / 10, 0.1, 9.9), 1)

def enrich(sig: dict) -> dict:
    sig["confidence_score"] = confidence_score(sig.get("validation"))
    sig["is_bet_recommendation"] = sig.get("color") in ("green", "blue", "yellow")
    sig["kelly_score"] = kelly_score(sig)
    sig["kelly_tier"], sig["kelly_stake"] = kelly_tier(sig["kelly_score"])
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

    # Linea de consenso por lado: la que tiene mas casas de acuerdo. Si otra
    # casa cotiza un point distinto (linea movida/alt), se etiqueta como
    # "(Alt)" en vez de aparecer como si fuera un mercado separado sin marcar.
    main_point: dict[str, float] = {}
    for (name, point), c in counts.items():
        if name not in main_point or c > counts.get((name, main_point[name]), 0):
            main_point[name] = point

    signals = []
    for (name, point), p in fair.items():
        best = best_price_filtered(outs, name, p, point=point)
        if not best:
            continue
        is_alt = point != main_point.get(name)
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        val   = smooth_validation(prob, odds, counts.get((name, point), 0), ev, edge)
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds)
        fd_odds   = fanduel_price(outs, name, point)
        point_str = (f"+{point}" if (point or 0) > 0 else str(point)) if point is not None else ""
        selection = f"{name} {point_str}".strip()
        if is_alt:
            selection += " (Alt)"
        signals.append(enrich({
            "mode":              "efficiency",
            "sport":             sport,
            "market":            "Spread",
            "short_market":      "SPR",
            "selection":         selection,
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
            "is_alt_line":       is_alt,
            "fanduel_odds":      fd_odds,
            "fanduel_available": fd_odds is not None,
            "point":             point,
        }))
    return sorted(signals, key=lambda x: (ranking_edge(x), x["validation"], x["probability"]), reverse=True)


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
    return sorted(signals, key=lambda x: (ranking_edge(x), x["validation"], x["probability"]), reverse=True)


# ---------------------------------------------------------------------------
# Predicción completa por juego (v8.5)
# ---------------------------------------------------------------------------
def best_market(ml: list, spread: list, total: list) -> Optional[dict]:
    """
    Elige el mercado con mejor EV real entre ML/Spread/Total, mismas
    compuertas que la clasificación SÓLIDO/PROBABLE (Prob>=58, Val>=60,
    EV>=piso): solo compite entre picks color green/blue. Si ningún
    mercado del partido pasa las compuertas, no hay recomendación (None)
    en vez de mostrar el menos malo con etiqueta EVITAR contradictoria.
    """
    def best_of(signals: list) -> Optional[dict]:
        cands = [
            s for s in (signals or [])
            if s.get("ev") is not None and s.get("color") in ("green", "blue")
        ]
        if not cands:
            return None
        return max(cands, key=ranking_edge)

    candidates = [
        (n, best_of(s)) for n, s in [("Moneyline", ml), ("Spread", spread), ("Total", total)]
    ]
    candidates = [(n, s) for n, s in candidates if s]
    if not candidates:
        return None
    # Comparar por 'ev' crudo entre mercados favorece sistematicamente a
    # Spread/Total (cuota decimal ~1.9) sobre un ML de favorito fuerte
    # (cuota decimal 1.3-1.7) aunque el edge real sea equivalente o mejor
    # en el ML. ranking_edge saca esa distorsion cross-market.
    best_name, best_sig = max(candidates, key=lambda x: ranking_edge(x[1]))
    return {
        **best_sig,
        "recommended_market": best_name,
        "reason": f"{best_name} — Val {best_sig.get('validation')}% / EV {best_sig.get('ev')}%",
        # Expuesto para que el front ordene la lista de juegos con el mismo
        # criterio que se usó acá adentro para elegir el mercado (antes
        # ordenaba por bm.ev crudo, reintroduciendo el sesgo en el cliente
        # aunque el backend ya eligiera bien). Ver ranking_edge.
        "rank_score": ranking_edge(best_sig),
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
# Ranking Kelly
# ---------------------------------------------------------------------------
def build_kelly_ranking(all_signals: list) -> tuple[list, Optional[dict]]:
    """
    Un pick por partido para la pestaña Ranking Kelly: el de mayor
    kelly_score entre ML/Spread/Total de ese partido. Solo entran los que
    superan KELLY_MIN_SCORE — por debajo, el partido directamente no
    aparece en el ranking (no se marca EVITAR, no hay fila para el).

    Si NINGUN partido llega al piso ese dia, devuelve el mas cercano aparte
    (near_miss) para que la pestaña pueda explicar el vacío en vez de
    mostrar nada sin contexto.
    """
    best_per_game: dict[str, dict] = {}
    for s in all_signals:
        game = s.get("game")
        if game is None:
            continue
        score = s.get("kelly_score") or 0.0
        if game not in best_per_game or score > (best_per_game[game].get("kelly_score") or 0.0):
            best_per_game[game] = s

    candidates = list(best_per_game.values())
    ranked = sorted(
        [s for s in candidates if (s.get("kelly_score") or 0.0) >= KELLY_MIN_SCORE],
        key=lambda s: s.get("kelly_score") or 0.0,
        reverse=True,
    )
    near_miss = None
    if not ranked and candidates:
        near_miss = max(candidates, key=lambda s: s.get("kelly_score") or -999.0)
    return ranked, near_miss

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

def ticket_ok_market(sig: Optional[dict]) -> bool:
    """Piso minimo para parlays que no filtran por color (Mixto, ML+Total):
    NO exige green/blue (siguen mostrando todo el mercado, no solo picks
    'curados'). Usa el mismo piso KELLY_MIN_SCORE que la pestaña Ranking
    Kelly a proposito: si un pick no alcanza para aparecer en Ranking
    Kelly, tampoco puede colarse en un parlay — mismo criterio en todo el
    sitio, sin picks contradictorios entre pestañas."""
    if not sig:
        return False
    return (sig.get("kelly_score") or 0.0) >= KELLY_MIN_SCORE

def _market_cap(count: int) -> int:
    """Tope de patas que puede aportar un solo mercado (~2/3 del ticket,
    piso 2) para que un mercado no monopolice todas las patas."""
    return max(2, -(-(count * 2) // 3))

def build_ticket(name: str, pool: list, count: int, ok_fn, color: str, risk: str, reason: str,
                  max_per_market: Optional[int] = None, max_legs_per_game: int = 1) -> Optional[dict]:
    picks = []
    game_leg_counts: dict[str, int] = {}
    game_market_seen: set = set()
    market_counts: dict[str, int] = {}

    def admissible(s):
        game = s.get("game")
        if game_leg_counts.get(game, 0) >= max_legs_per_game:
            return False
        if (game, s.get("market")) in game_market_seen:
            return False
        return True

    def take(s):
        picks.append(s)
        game = s.get("game")
        game_leg_counts[game] = game_leg_counts.get(game, 0) + 1
        game_market_seen.add((game, s.get("market")))
        m = s.get("market")
        market_counts[m] = market_counts.get(m, 0) + 1

    # Primera pasada: respeta el tope por mercado (diversifica cuando hay datos).
    for s in pool:
        if len(picks) >= count:
            break
        if not admissible(s) or not ok_fn(s):
            continue
        if max_per_market is not None and market_counts.get(s.get("market"), 0) >= max_per_market:
            continue
        take(s)

    # Segunda pasada: si no alcanzó el count por falta de diversidad ese día,
    # completa ignorando el tope — mejor rellenar por EV real que dejar el
    # ticket corto por una diversidad que los datos no dan.
    if len(picks) < count:
        for s in pool:
            if len(picks) >= count:
                break
            if not admissible(s) or not ok_fn(s):
                continue
            take(s)

    real_count = len(picks)
    if real_count < 3:
        return None

    insufficient = real_count < count
    dynamic_name = name.replace(str(count), str(real_count)) if str(count) in name else f"{name} ({real_count})"
    if insufficient:
        dynamic_reason = f"Solo {real_count} disponibles hoy (se buscaban {count})."
    elif str(count) in reason:
        dynamic_reason = reason.replace(str(count), str(real_count))
    else:
        dynamic_reason = reason

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
        "insufficient":         insufficient,
        "validation":           round(clamp(avg, *VAL_CLAMP), 1),
        "combined_probability": round(clamp(comb * 100, 0.1, 95), 1),
        "risk":                 risk,
        "reason":               dynamic_reason,
    }

def group_picks_by_game(picks: list) -> list:
    """Reordena las patas de un ticket para que las de un mismo partido
    queden juntas (una debajo de la otra) en vez de intercaladas con las
    de otros partidos. Los grupos se ordenan por el mejor kelly_score del
    par; dentro de un grupo se conserva el orden de entrada. Solo cambia
    el orden de visualización, no descarta ni recalcula nada."""
    groups: dict = {}
    for p in picks:
        groups.setdefault(p.get("game"), []).append(p)
    ordered_games = sorted(
        groups.keys(),
        key=lambda g: max((x.get("kelly_score") or 0.0) for x in groups[g]),
        reverse=True,
    )
    return [p for g in ordered_games for p in groups[g]]

def build_mltotal_ticket(name: str, pool: list, games_target: int, color: str, risk: str, reason: str) -> Optional[dict]:
    """
    A diferencia de build_ticket (arma la lista pata por pata rankeando por
    kelly_score sobre todo el pool), este arma el parlay ML+Total eligiendo
    PARTIDOS por el mejor kelly_score combinado (ML+Total de ese partido)
    e incluye siempre AMBAS patas del partido elegido — nunca un partido
    suelto con una sola pata. Solo entran partidos donde al menos una pata
    supera KELLY_MIN_SCORE: mismo piso que Ranking Kelly y Parlay Mixto,
    para no mostrar acá un partido que ahí no calificaría.
    'games_target' es el número de PARTIDOS que arma el parlay (no patas):
    cada partido aporta hasta 2 patas (ML+Total), así que el total de patas
    del ticket es normalmente 2x games_target (menos si a algún partido
    elegido le falta uno de los dos mercados hoy).
    """
    games: dict[str, list] = {}
    for s in pool:
        games.setdefault(s.get("game"), []).append(s)

    def best_per_market(sigs: list) -> list:
        by_market: dict = {}
        for s in sigs:
            m = s.get("market")
            if m not in by_market or (s.get("kelly_score") or 0.0) > (by_market[m].get("kelly_score") or 0.0):
                by_market[m] = s
        return list(by_market.values())

    game_legs = {g: best_per_market(sigs) for g, sigs in games.items()}
    qualifying_games = [
        g for g, legs in game_legs.items()
        if max((l.get("kelly_score") or 0.0) for l in legs) >= KELLY_MIN_SCORE
    ]
    combined_rank = lambda g: sum((l.get("kelly_score") or 0.0) for l in game_legs[g])
    ordered_games = sorted(qualifying_games, key=combined_rank, reverse=True)

    selected_games = ordered_games[:games_target]
    picks = []
    for g in selected_games:
        picks.extend(game_legs[g])

    games_count = len(selected_games)
    legs_count  = len(picks)
    if games_count < min(games_target, 2):
        return None

    picks = group_picks_by_game(picks)
    insufficient  = games_count < games_target
    dynamic_name  = name.replace(str(games_target), str(games_count)) if str(games_target) in name else f"{name} ({games_count})"
    if insufficient:
        dynamic_reason = f"Solo {games_count} juegos disponibles hoy (se buscaban {games_target})."
    elif str(games_target) in reason:
        dynamic_reason = reason.replace(str(games_target), str(games_count))
    else:
        dynamic_reason = reason

    comb = 1.0
    avg  = 0.0
    for x in picks:
        comb *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg  += x.get("validation", 0)
    avg /= legs_count

    return {
        "name":                 dynamic_name,
        "color":                color,
        "picks":                picks,
        "picks_count":          legs_count,
        "games_count":          games_count,
        "target_count":         games_target,
        "insufficient":         insufficient,
        "validation":           round(clamp(avg, *VAL_CLAMP), 1),
        "combined_probability": round(clamp(comb * 100, 0.1, 95), 1),
        "risk":                 risk,
        "reason":               dynamic_reason,
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
        "all_games":        [],

        # Eficiencia (MLB/NBA/NHL) — TODAS las señales del día (ML/Spread/
        # Total, todos los partidos), coloreadas según su clasificación real
        # (classify_efficiency decide el color, no la visibilidad). Estas
        # listas nunca se filtran ni se vacían por "no hay SÓLIDO/PROBABLE
        # suficiente": siempre muestran el mercado completo, por EV.
        "efficiency_green":  [],
        "efficiency_blue":   [],
        "efficiency_yellow": [],
        "avoid": [],

        # Ranking Kelly — pestaña nueva en paralelo (ver build_kelly_ranking).
        # Un pick por partido, solo si supera KELLY_MIN_SCORE.
        "kelly_ranking":  [],
        "kelly_near_miss": None,

        # Tickets
        "ticket_efficiency": None,

        "warnings": [],
    }

    for label, key in SPORTS_EFFICIENCY.items():
        if label not in selected_sports and "ALL" not in selected_sports:
            continue
        try:
            games, from_cache, ttl = fetch_odds(key, force_refresh=force_refresh)
            total = len(games)
            games = [g for g in games if is_game_today(g) and is_upcoming(g)]
            dash["sports"][label] = {
                "ok": True, "sport_key": key,
                "games_count": len(games), "total_api_games": total,
                "from_cache": from_cache, "cache_seconds_left": ttl,
            }
            for g in games:
                dash["all_games"].append(game_prediction_full(g, label))
        except Exception as e:
            logger.error("Error %s: %s", label, e, exc_info=True)
            dash["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
            dash["warnings"].append(f"{label}: {e}")

    sorter_ev   = lambda x: x.get("ev") if x.get("ev") is not None else -999
    # Para listas/pools SIN piso de probabilidad (avoid, parlays sin color):
    # ranking_edge en vez de 'ev' crudo, para que una cuota alta no empuje
    # arriba un pick solo por el multiplicador de pago (ver ranking_edge).
    # SÓLIDO/PROBABLE quedan con sorter_ev: ya estan protegidos por el piso
    # de probabilidad de classify_efficiency, ahi 'ev' no tiene el sesgo.
    sorter_rank = lambda x: ranking_edge(x)
    # Tickets/Parlays y Ranking Kelly comparten este criterio (ver
    # kelly_score): el mismo número decide qué aparece y en qué orden en
    # las dos pestañas, para que no se contradigan entre sí.
    sorter_kelly = lambda x: x.get("kelly_score") or 0.0

    # Todas las señales del día en un solo pool — base de Eficiencia, Alta
    # Prob y Parlays. No se colapsa a "1 mejor pick por juego": cada
    # mercado de cada partido aparece con su color real.
    all_signals = []
    for full in dash["all_games"]:
        for mkey in ("ml", "spread", "total"):
            for sig in full.get(mkey, []):
                all_signals.append({**sig, "game": full["game"], "start_time": full["start_time"]})
    all_signals = dedupe(all_signals)

    dash["kelly_ranking"], dash["kelly_near_miss"] = build_kelly_ranking(all_signals)

    dash["efficiency_green"]  = sorted([s for s in all_signals if s.get("color") == "green"],  key=sorter_ev, reverse=True)
    dash["efficiency_blue"]   = sorted([s for s in all_signals if s.get("color") == "blue"],   key=sorter_ev, reverse=True)
    # yellow (ESPECULATIVO) ya exige edge>=0 para entrar (ver classify_efficiency),
    # pero dentro del tier se ordena por ranking_edge igual que avoid: el piso
    # de probabilidad es mas bajo que green/blue, asi que el sesgo de EV hacia
    # cuota alta que ranking_edge corrige pesa mas aca en el orden interno.
    dash["efficiency_yellow"] = sorted([s for s in all_signals if s.get("color") == "yellow"], key=sorter_rank, reverse=True)
    dash["avoid"]             = sorted([s for s in all_signals if s.get("color") == "red"],    key=sorter_rank, reverse=True)

    # Ticket Eficiencia: producto curado (requiere green/blue), queda
    # deshabilitado en la UI (display:none) pero se mantiene funcional.
    eff_pool = dedupe(dash["efficiency_green"] + dash["efficiency_blue"])
    dash["ticket_efficiency"] = build_ticket(
        "🔒 Ticket Eficiencia", eff_pool, 6, ticket_ok_efficiency,
        "green", "Bajo", "Picks de alta probabilidad en MLB/NBA/NHL."
    )

    # Parlay Mixto (3/6/10 legs) — ML + Spread + Total, top Score Kelly del
    # día sin filtrar por color (ticket_ok_market usa el mismo
    # KELLY_MIN_SCORE que Ranking Kelly, no exige green/blue: sigue
    # mostrando todo el mercado que califica). Ordenado por kelly_score, el
    # mismo criterio que Ranking Kelly, para que ninguna de las dos
    # pestañas contradiga a la otra. Solo devuelve None si literalmente no
    # hay 3 patas que superen el piso hoy (build_ticket ya lo garantiza).
    # El tope por mercado evita que ML monopolice las patas cuando hay
    # Spread/Total disponibles.
    _parlay_pool = sorted(all_signals, key=sorter_kelly, reverse=True)
    dash["ticket_3"]  = build_ticket("🎯 Parlay Mixto — 3 Legs",  _parlay_pool, 3,  ticket_ok_market, "yellow", "Bajo",  "3 mejores picks del día por Score Kelly (ML/Spread/Total).", max_per_market=_market_cap(3))
    dash["ticket_6"]  = build_ticket("🔥 Parlay Mixto — 6 Legs",  _parlay_pool, 6,  ticket_ok_market, "yellow", "Medio", "6 mejores picks del día por Score Kelly (ML/Spread/Total).", max_per_market=_market_cap(6))
    dash["ticket_10"] = build_ticket("⭐ Parlay Mixto — 10 Legs", _parlay_pool, 10, ticket_ok_market, "yellow", "Alto",  "10 mejores picks del día por Score Kelly (ML/Spread/Total).", max_per_market=_market_cap(10))

    # Parlay ML + Total (3/6/10 legs) — excluye Spread. Elige PARTIDOS por
    # el mejor Score Kelly combinado (ML+Total de ese partido, ver
    # build_mltotal_ticket), no patas sueltas por EV individual: así
    # siempre entran ambas patas del partido elegido, y solo entran
    # partidos donde al menos una pata supera KELLY_MIN_SCORE (mismo piso
    # que Ranking Kelly y Parlay Mixto).
    _parlay_pool_mltotal = [s for s in all_signals if s.get("market") != "Spread"]
    dash["ticket_mltotal_3"]  = build_mltotal_ticket("🎯 Parlay ML+Total — 3 Juegos",  _parlay_pool_mltotal, 3,  "yellow", "Bajo",  "3 mejores juegos del día por Score Kelly combinado (ML+Total).")
    dash["ticket_mltotal_6"]  = build_mltotal_ticket("🔥 Parlay ML+Total — 6 Juegos",  _parlay_pool_mltotal, 6,  "yellow", "Medio", "6 mejores juegos del día por Score Kelly combinado (ML+Total).")
    dash["ticket_mltotal_10"] = build_mltotal_ticket("⭐ Parlay ML+Total — 10 Juegos", _parlay_pool_mltotal, 10, "yellow", "Alto",  "10 mejores juegos del día por Score Kelly combinado (ML+Total).")

    # Alta Prob: prioriza ≥65% (su propósito), pero nunca vacía la pestaña
    # por eso. by_prob ya viene ordenado descendente por probabilidad, asi
    # que los ≥65% (si hay) quedan siempre primero en by_prob[:20] — no hace
    # falta un fallback aparte. El bug anterior era `(high_prob_65 or
    # by_prob)[:20]`: si high_prob_65 tenia 1-19 items (no 0), se mostraban
    # SOLO esos en vez de rellenar hasta 20 con el resto del dia — un dia
    # con 5 picks ≥65% mostraba 5 en vez de 20. high_prob_goal_count expone
    # cuantos de los mostrados alcanzan el objetivo real, para que el
    # frontend distinga "el mejor disponible hoy" de "cumple el objetivo".
    by_prob = sorted(all_signals, key=lambda x: x.get("probability") or 0, reverse=True)
    dash["high_prob"] = by_prob[:20]
    dash["high_prob_goal_count"] = sum(1 for p in by_prob if (p.get("probability") or 0) >= 65.0)

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
            games = [g for g in games if is_game_today(g) and is_upcoming(g)]
            res["sports"][label] = {
                "ok": True, "sport_key": key,
                "games_count": len(games), "total_api_games": total,
                "from_cache": fc, "cache_seconds_left": ttl,
            }
        except Exception as e:
            res["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
    return res
