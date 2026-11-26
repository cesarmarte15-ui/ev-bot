"""
ev_engine_v8.5.py
- MLB/NBA/NHL: modo EFICIENCIA - favoritos sólidos, difícil perder
- v8.5: fallback ML→Spread/Total, umbrales más flexibles
"""

import os
import json
import math
import time
import logging
import threading
import requests
from collections import Counter
from datetime import datetime, timedelta, timezone, date
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
API_BASE: str = "https://api.the-odds-api.com/v4"

# Catálogo completo de ligas (etiqueta -> sport key de The Odds API). Solo se
# piden cuotas (/odds, cuesta créditos) de las que tienen juegos por empezar
# hoy según /sports y /events (gratis) — ver active_league_events().
ALL_LEAGUES: dict[str, str] = {
    "NFL":          "americanfootball_nfl",
    "NCAAF":        "americanfootball_ncaaf",
    "NCAAB":        "basketball_ncaab",
    "NBA":          "basketball_nba",
    "NHL":          "icehockey_nhl",
    "MLB":          "baseball_mlb",
    "EPL":          "soccer_epl",
    "La Liga":      "soccer_spain_la_liga",
    "Champions":    "soccer_uefa_champs_league",
    "Liga MX":      "soccer_mexico_ligamx",
    # Ligas menos seguidas: las casas de EE.UU. les dedican menos atención,
    # así que sus precios se alejan más del de Pinnacle.
    "Championship": "soccer_efl_champ",
    "Portugal":     "soccer_portugal_primeira_liga",
    "Eredivisie":   "soccer_netherlands_eredivisie",
}

def _env_leagues() -> dict[str, str]:
    """LEAGUES=NFL,NBA,EPL (etiquetas de ALL_LEAGUES) para limitar la lista
    sin tocar código. Vacío = todas."""
    raw = os.getenv("LEAGUES", "").strip()
    if not raw:
        return dict(ALL_LEAGUES)
    wanted = [x.strip() for x in raw.split(",") if x.strip()]
    return {k: v for k, v in ALL_LEAGUES.items() if k in wanted}

SPORTS_EFFICIENCY: dict[str, str] = _env_leagues()
SPORTS: dict[str, str] = SPORTS_EFFICIENCY

REGIONS: str     = os.getenv("REGIONS", "us")
MARKETS: str     = os.getenv("MARKETS", "h2h,spreads,totals")
ODDS_FORMAT: str = "american"
LOCAL_TZ: str    = os.getenv("LOCAL_TZ", "America/New_York")
ONLY_TODAY: bool = os.getenv("ONLY_TODAY", "1") == "1"

# Casas pedidas a la API. Con el parámetro 'bookmakers' cada grupo de hasta
# 10 casas cuesta lo mismo que 1 región, así que Pinnacle (región eu) entra
# sin pagar una segunda región. Si BOOKMAKERS queda vacío se usa REGIONS.
# Caesars en The Odds API es 'williamhill_us'. bet365 no está disponible
# para EE.UU. en The Odds API (solo bet365_au), por eso no se pide.
BOOKMAKERS: list[str] = [b.strip() for b in os.getenv(
    "BOOKMAKERS",
    "pinnacle,fanduel,draftkings,betmgm,williamhill_us,fanatics,betrivers,lowvig,betonlineag",
).split(",") if b.strip()]

# Casas donde el usuario puede apostar (legales en NJ). El MEJOR PRECIO de
# cada señal se busca solo entre estas; Pinnacle/LowVig/BetOnline se usan
# como ancla del precio justo y para corroboración (book_count), nunca como
# casa recomendada. 'caesars' y 'bet365' quedan por si la API los devuelve
# con esa clave.
BETTABLE_BOOKS: set[str] = {b.strip() for b in os.getenv(
    "BETTABLE_BOOKS",
    "fanduel,draftkings,betmgm,williamhill_us,caesars,fanatics,betrivers,bet365",
).split(",") if b.strip()}

# Intervalo de actualización de cuotas (minutos). Es el TTL de la caché de
# /odds: ninguna liga se vuelve a pedir antes de que pase. Mínimo 20.
REFRESH_INTERVAL_MIN: int = max(20, int(os.getenv("REFRESH_INTERVAL_MIN", "45")))
CACHE_TTL: int = REFRESH_INTERVAL_MIN * 60
# El botón "Actualizar" solo vuelve a pedir una liga si sus cuotas tienen
# más de estos minutos (evita quemar créditos con clicks repetidos).
MIN_FORCE_REFRESH_MIN: int = int(os.getenv("MIN_FORCE_REFRESH_MIN", "10"))
# TTL de /sports y /events (gratis, pero no hace falta pedirlos en cada carga).
EVENTS_TTL: int = int(os.getenv("EVENTS_TTL_MIN", "30")) * 60
SPORTS_LIST_TTL: int = 6 * 3600

# Presupuesto de créditos. MONTHLY_CREDIT_BUDGET es la meta de consumo
# (debajo del total del plan, con margen). BILLING_RESET_DAY = día del mes
# en que The Odds API reinicia la cuota (vacío = se asume el día 1).
MONTHLY_CREDIT_BUDGET: int = int(os.getenv("MONTHLY_CREDIT_BUDGET", "18000"))
PLAN_CREDITS: int = int(os.getenv("PLAN_CREDITS", "20000"))
BILLING_RESET_DAY: Optional[int] = int(os.getenv("BILLING_RESET_DAY")) if os.getenv("BILLING_RESET_DAY", "").strip() else None
BUDGET_GUARD: bool = os.getenv("BUDGET_GUARD", "1") == "1"
LOW_CREDITS_PCT: float = 20.0
# Modo ahorro: solo se activa cuando x-requests-remaining baja de este número.
MIN_REMAINING_CREDITS: int = 50
USAGE_FILE: str = os.getenv("USAGE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_usage.json"))

# Cuotas Altas: underdogs con EV positivo contra Pinnacle.
HIGH_ODDS_MIN: int   = int(os.getenv("HIGH_ODDS_MIN", "150"))
HIGH_ODDS_MAX: int   = int(os.getenv("HIGH_ODDS_MAX", "500"))
HIGH_ODDS_LIMIT: int = int(os.getenv("HIGH_ODDS_LIMIT", "15"))
HIGH_ODDS_MIN_EDGE: float = float(os.getenv("HIGH_ODDS_MIN_EDGE", "1.0"))   # puntos de probabilidad
TOP_PLAYS_COUNT: int = int(os.getenv("TOP_PLAYS_COUNT", "5"))
TOP_PLAYS_MAX_ODDS: int = int(os.getenv("TOP_PLAYS_MAX_ODDS", "500"))

# Umbrales
EFFICIENCY_MIN_PROB     = 60.0   # % mínimo para pick eficiente
EFFICIENCY_MIN_VAL      = 63.0   # validación mínima
EFFICIENCY_MAX_ODDS     = -180   # no más caro que -180 en americano para eficiencia
# Piso para SÓLIDO/PROBABLE en 'edge' (puntos de probabilidad), no 'ev'
# crudo. classify_efficiency usaba ev >= 0.0 / >= -1.0: mismo sesgo de
# cuota alta que a29c99f ya corrigio para ranking_edge (ev = edge *
# decimal_odds, así que una cuota corta de favorito comprime el
# multiplicador y ev crudo rara vez llega a positivo aunque el edge real
# sea bueno). Verificado contra 13 dias de capturas reales de produccion
# (13-26 ago 2026, 1790 señales): prob>=60 y ev>=0 simultaneo, 0 veces en
# los 13 dias (0 SÓLIDO); con edge en vez de ev crudo el piso deja de
# estar sesgado contra los favoritos que prob>=60 exige.
EFFICIENCY_MIN_EDGE_GREEN = -1.0   # piso de edge para SÓLIDO
EFFICIENCY_MIN_EDGE_BLUE  = -1.5   # piso de edge para PROBABLE

# Piso de EV real (no edge, no 'validacion') para SÓLIDO/PROBABLE. Hasta acá
# classify_efficiency no exigia 'ev' para estos dos tiers (solo edge/prob/
# val), lo que permitia que un favorito con EV negativo pero edge/val altos
# calificara SÓLIDO mientras un underdog con EV positivo pero prob/val bajos
# (estructuralmente bajos: 'val' arranca de la probabilidad implicita, que
# para un underdog nunca es alta) terminaba en EVITAR — exactamente al
# reves de lo que un bot de Expected Value deberia mostrar. Diagnosticado
# contra 33 capturas reales de produccion (13 ago - 4 sep 2026): 24 señales
# de favorito con EV negativo clasificadas SÓLIDO/PROBABLE, 0 de 40 señales
# de underdog con EV positivo llegaron siquiera a PROBABLE. edge/prob/val
# se mantienen como compuertas adicionales (corroboracion cruzada, calidad
# de precio), pero ya no alcanzan por si solas para pisar el EV real.
EFFICIENCY_MIN_EV_GREEN = 0.0    # SÓLIDO exige EV real >= 0
EFFICIENCY_MIN_EV_BLUE  = -1.0   # PROBABLE tolera EV negativo hasta -1.0%
# Ampliado de -0.5 a -1.0 (ver PROBATE_FALLBACK_* abajo): objetivo es
# mostrar mas picks reales basados en EV sin bajar el piso de SOLIDO.
# Simulado contra 33 capturas reales de produccion (13 ago - 4 sep 2026,
# 2018 señales): con -1.0, 53 señales que antes quedaban en el tier
# ESPECULATIVO (ver mas abajo) pasan a PROBABLE con EV% promedio +0.66%;
# las 21 restantes (EV% promedio -1.31%) correctamente quedan en EVITAR.

# Piso de prob/val del tier ESPECULATIVO (eliminado como tier propio, ver
# classify_efficiency): se mantiene como el gate que decide QUE señales
# "ex-especulativas" pueden calificar para PROBABLE sin exigirles el gate
# normal de PROBABLE (prob>=58/val>=60/edge>=-1.5). Antes definia un tier
# aparte con su propio piso de EV (-1.5); ese piso de EV ahora es el mismo
# EFFICIENCY_MIN_EV_BLUE que el resto de PROBATE — todo lo que caiga entre
# -1.5 y -1.0 pasa a EVITAR en vez de a un tier intermedio.
PROBABLE_FALLBACK_MIN_PROB = 55.0
PROBABLE_FALLBACK_MIN_VAL  = 58.0

# Piso de casas de acuerdo para SÓLIDO/PROBABLE/ESPECULATIVO. Sin esto, un
# precio de una sola casa (potencialmente un outlier o una línea stale) podía
# calificar con el mismo peso que un consenso de 9 casas — nada en
# classify_efficiency exigía corroboración cruzada (a diferencia de Kelly/
# Tickets, que ya atenúan por book_count via 'confidence'). Verificado contra
# 32 capturas reales de produccion (13 ago - 1 sep 2026, 1928 señales): el
# 100% de las señales SÓLIDO/PROBABLE/ESPECULATIVO ya tenian book_count >= 5
# (nunca bajo 3) — el piso de prob/val/edge ya filtraba indirectamente los
# mercados finos, asi que este piso no cambia ningun resultado historico,
# solo cierra el hueco estructural para el dia que si aparezca un precio de
# una sola casa.
EFFICIENCY_MIN_BOOKS = 3

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

def _cache_get(key: str, ttl: Optional[int] = None) -> Optional[tuple[list, int]]:
    ttl = CACHE_TTL if ttl is None else ttl
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, data = entry
        remaining = ttl - (time.time() - ts)
        if remaining <= 0:
            return None
        return data, int(remaining)

def _cache_age(key: str) -> Optional[float]:
    """Segundos desde que se guardó la entrada (vencida o no), None si no hay."""
    with _cache_lock:
        entry = _cache.get(key)
        return None if entry is None else time.time() - entry[0]

def _cache_get_stale(key: str) -> Optional[list]:
    """Devuelve la entrada aunque esté vencida (modo ahorro de créditos)."""
    with _cache_lock:
        entry = _cache.get(key)
        return None if entry is None else entry[1]

def _cache_set(key: str, data: list) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), data)

# ---------------------------------------------------------------------------
# Histéresis de tier SÓLIDO (ver classify_efficiency) — último tier
# confirmado por pick (game+market+selection), en memoria, sobrevive a
# force_refresh a propósito: el objetivo es amortiguar el parpadeo entre
# escaneos, así que limpiar la caché de cuotas no debe resetear esto.
# ---------------------------------------------------------------------------
EFFICIENCY_GREEN_EXIT_EV_BUFFER   = 0.5
EFFICIENCY_GREEN_EXIT_EDGE_BUFFER = 0.5

_tier_lock = threading.Lock()
_tier_state: dict[str, str] = {}

def _prev_tier(pick_id: Optional[str]) -> Optional[str]:
    if not pick_id:
        return None
    with _tier_lock:
        return _tier_state.get(pick_id)

def _remember_tier(pick_id: Optional[str], color: str) -> None:
    if not pick_id:
        return
    with _tier_lock:
        _tier_state[pick_id] = color

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

# ---------------------------------------------------------------------------
# Consumo de créditos (headers x-requests-* de The Odds API)
# ---------------------------------------------------------------------------
# Cada respuesta de la API (incluso /sports y /events, que no cuestan) trae
# x-requests-remaining / x-requests-used / x-requests-last. Se guardan en
# memoria y en USAGE_FILE para que un reinicio no deje el contador en blanco.
# 'spent_today' suma x-requests-last de las llamadas a /odds del día local
# (solo informativo; el guardián usa x-requests-used, ver budget_pace).
_usage_lock = threading.Lock()
_usage: dict = {
    "remaining": None, "used": None, "last": None, "updated_at": None,
    "day": None, "spent_today": 0,
}

def _load_usage() -> None:
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in _usage:
                if k in data:
                    _usage[k] = data[k]
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("No se pudo leer %s: %s", USAGE_FILE, e)

def _save_usage_locked() -> None:
    try:
        tmp = USAGE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_usage, f)
        os.replace(tmp, USAGE_FILE)
    except Exception as e:
        logger.warning("No se pudo guardar %s: %s", USAGE_FILE, e)

_load_usage()

def _local_today() -> date:
    return datetime.now(ZoneInfo(LOCAL_TZ)).date()

def _roll_day_locked() -> None:
    today = _local_today().isoformat()
    if _usage.get("day") != today:
        _usage["day"] = today
        _usage["spent_today"] = 0

def _header_int(headers, name: str) -> Optional[int]:
    try:
        return int(float(headers.get(name)))
    except (TypeError, ValueError):
        return None

def _record_usage(r, counts_spend: bool) -> None:
    rem  = _header_int(r.headers, "x-requests-remaining")
    used = _header_int(r.headers, "x-requests-used")
    last = _header_int(r.headers, "x-requests-last")
    with _usage_lock:
        _roll_day_locked()
        if rem is not None:
            _usage["remaining"] = rem
        if used is not None:
            _usage["used"] = used
        if last is not None:
            _usage["last"] = last
        if rem is not None or used is not None:
            _usage["updated_at"] = datetime.now(timezone.utc).isoformat()
        if counts_spend and last:
            _usage["spent_today"] = (_usage.get("spent_today") or 0) + last
        _save_usage_locked()

def odds_call_cost() -> int:
    """Créditos que cuesta un GET /odds: mercados x regiones, donde con
    'bookmakers' cada grupo de hasta 10 casas cuenta como 1 región."""
    n_markets = len([m for m in MARKETS.split(",") if m.strip()])
    if BOOKMAKERS:
        groups = math.ceil(len(BOOKMAKERS) / 10)
    else:
        groups = len([x for x in REGIONS.split(",") if x.strip()])
    return max(1, n_markets) * max(1, groups)

def _billing_cycle(today: date) -> tuple[date, date]:
    """(inicio, fin) del ciclo de facturación que contiene 'today': del
    último día de reinicio (BILLING_RESET_DAY, o el 1 si no está
    configurado) al siguiente. Días 29-31 se ajustan al fin de mes."""
    import calendar
    day = BILLING_RESET_DAY or 1
    def on(y: int, m: int) -> date:
        return date(y, m, min(day, calendar.monthrange(y, m)[1]))
    def shift(y: int, m: int, k: int) -> tuple[int, int]:
        m0 = (y * 12 + (m - 1)) + k
        return m0 // 12, m0 % 12 + 1
    this_month = on(today.year, today.month)
    if this_month <= today:
        start, end = this_month, on(*shift(today.year, today.month, 1))
    else:
        start, end = on(*shift(today.year, today.month, -1)), this_month
    return start, end

def budget_pace() -> dict:
    """
    Guardián de presupuesto por RITMO, basado en x-requests-used (gasto
    acumulado del ciclo que reporta la propia API). v8.7 contaba el gasto
    del día en memoria/archivo; en Render el servicio se reinicia (duerme,
    redeploys) y el disco es efímero, así que ese conteo volvía a 0 y el
    guardián quedaba más flojo de lo previsto. Ahora no hay estado propio:

      presupuesto  = (remaining + used) * MONTHLY_CREDIT_BUDGET/PLAN_CREDITS
      permitido    = presupuesto * dias_transcurridos / dias_del_ciclo
      modo ahorro  si used + costo > permitido

    (dias_transcurridos incluye hoy). Ej. 450 de presupuesto en un ciclo de
    30 días: día 1 permite 15, día 10 permite 150, último día permite 450.
    Lo no gastado un día queda disponible los siguientes.
    """
    with _usage_lock:
        remaining = _usage.get("remaining")
        used      = _usage.get("used")
    today = _local_today()
    start, end = _billing_cycle(today)
    cycle_len = max(1, (end - start).days)
    day_idx = min(cycle_len, (today - start).days + 1)
    out = {
        "cycle_start": start.isoformat(), "cycle_end": end.isoformat(),
        "cycle_day": day_idx, "cycle_len": cycle_len,
        "budget": None, "allowed_used": None, "headroom": None,
    }
    if remaining is None or used is None:
        return out
    total = remaining + used
    ratio = clamp(MONTHLY_CREDIT_BUDGET / PLAN_CREDITS, 0.01, 1.0) if PLAN_CREDITS > 0 else 0.9
    budget = total * ratio
    allowed = budget * day_idx / cycle_len
    out.update({
        "budget": int(budget),
        "allowed_used": int(allowed),
        "headroom": int(allowed - used),
    })
    return out

def budget_allows(cost: int) -> bool:
    if not BUDGET_GUARD:
        return True
    # Modo ahorro solo por créditos restantes en la cuenta (x-requests-remaining);
    # el ritmo de gasto (budget_pace) ya no bloquea, queda solo informativo.
    with _usage_lock:
        remaining = _usage.get("remaining")
    if remaining is None:
        # Sin headers todavía (recién reiniciado): /sports, que no cuesta y
        # se pide antes que cualquier /odds, los trae en la misma carga.
        return True
    return remaining >= MIN_REMAINING_CREDITS

def budget_reason() -> str:
    with _usage_lock:
        remaining = _usage.get("remaining")
    if remaining is None:
        return ""
    reset = f"día {BILLING_RESET_DAY}" if BILLING_RESET_DAY else "día 1 (BILLING_RESET_DAY no configurado)"
    return (f"Quedan {remaining} créditos en la cuenta (menos de {MIN_REMAINING_CREDITS}); "
            f"se reanuda al reiniciar el ciclo ({reset}).")

def usage_status() -> dict:
    with _usage_lock:
        _roll_day_locked()
        u = dict(_usage)
    remaining, used = u.get("remaining"), u.get("used")
    total = (remaining + (used or 0)) if remaining is not None else None
    pct = round(remaining / total * 100, 1) if total else None
    cost = odds_call_cost()
    pace = budget_pace()
    saving = bool(BUDGET_GUARD and remaining is not None
                  and remaining < MIN_REMAINING_CREDITS)
    return {
        "remaining":            remaining,
        "used":                 used,
        "total":                total,
        "pct_remaining":        pct,
        "low":                  pct is not None and pct < LOW_CREDITS_PCT,
        "low_threshold_pct":    LOW_CREDITS_PCT,
        "last_cost":            u.get("last"),
        "updated_at":           u.get("updated_at"),
        # Informativo (se pierde si el servicio se reinicia); el guardián
        # usa x-requests-used, ver budget_pace().
        "spent_today":          u.get("spent_today") or 0,
        "pace":                 pace,
        "saving_mode":          saving,
        "saving_reason":        budget_reason() if saving else "",
        "cost_per_league":      cost,
        "refresh_interval_min": REFRESH_INTERVAL_MIN,
        "billing_reset_day":    BILLING_RESET_DAY,
        "monthly_budget":       MONTHLY_CREDIT_BUDGET,
        "plan_credits":         PLAN_CREDITS,
        "budget_guard":         BUDGET_GUARD,
    }

# ---------------------------------------------------------------------------
# Llamadas HTTP
# ---------------------------------------------------------------------------
class BudgetExceeded(RuntimeError):
    pass

def _api_get(path: str, params: dict, counts_spend: bool = False):
    if not API_KEY or API_KEY == "pon_tu_api_key_aqui":
        raise RuntimeError("Falta ODDS_API_KEY en Render Environment")
    r = requests.get(f"{API_BASE}{path}", params={"apiKey": API_KEY, **params}, timeout=30)
    _record_usage(r, counts_spend)
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"The Odds API error {r.status_code}: {detail}")
    return r.json()

def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def today_window() -> tuple[str, Optional[str]]:
    """(desde, hasta) en UTC para commenceTimeFrom/To: desde ahora (los
    partidos ya empezados se descartan igual, ver is_upcoming) hasta el fin
    del día local. Sin tope si ONLY_TODAY=0."""
    now = datetime.now(timezone.utc)
    if not ONLY_TODAY:
        return _iso_z(now), None
    z = ZoneInfo(LOCAL_TZ)
    local_now = now.astimezone(z)
    end = datetime.combine(local_now.date() + timedelta(days=1), datetime.min.time(), tzinfo=z)
    return _iso_z(now), _iso_z(end - timedelta(seconds=1))

def fetch_active_sport_keys() -> Optional[set]:
    """Sport keys en temporada según GET /sports (no cuesta créditos).
    None si la llamada falla (en ese caso no se descarta ninguna liga)."""
    cached = _cache_get("__sports__", SPORTS_LIST_TTL)
    if cached:
        return set(cached[0])
    try:
        data = _api_get("/sports", {})
    except Exception as e:
        logger.warning("No se pudo leer /sports: %s", e)
        return None
    keys = [s.get("key") for s in data if s.get("active")]
    _cache_set("__sports__", keys)
    return set(keys)

def fetch_upcoming_events(sport_key: str) -> Optional[list]:
    """Partidos de hoy que todavía no empezaron, según GET /events (no
    cuesta créditos). None si la llamada falla."""
    ck = f"__events__:{sport_key}"
    cached = _cache_get(ck, EVENTS_TTL)
    if cached:
        events = cached[0]
    else:
        frm, to = today_window()
        params = {"dateFormat": "iso", "commenceTimeFrom": frm}
        if to:
            params["commenceTimeTo"] = to
        try:
            events = _api_get(f"/sports/{sport_key}/events", params)
        except Exception as e:
            logger.warning("No se pudo leer /events de %s: %s", sport_key, e)
            return None
        _cache_set(ck, events)
    return [e for e in events if is_game_today(e) and is_upcoming(e)]

_fetch_lock = threading.Lock()

def fetch_odds(sport_key: str, force_refresh: bool = False) -> tuple[list, bool, int, bool]:
    """
    Cuotas de una liga. Devuelve (juegos, from_cache, seg_cache_restantes, stale).
    - Caché de REFRESH_INTERVAL_MIN por liga.
    - force_refresh solo vuelve a pedir si las cuotas tienen más de
      MIN_FORCE_REFRESH_MIN.
    - Si el cupo diario está agotado (budget_allows) sirve la última copia
      aunque esté vencida (stale=True); si no hay ninguna, BudgetExceeded.
    - El lock evita que dos cargas simultáneas paguen la misma liga dos veces.
    """
    cache_key = f"{sport_key}:odds"
    with _fetch_lock:
        if force_refresh:
            age = _cache_age(cache_key)
            if age is not None and age < MIN_FORCE_REFRESH_MIN * 60:
                force_refresh = False
        if not force_refresh:
            cached = _cache_get(cache_key)
            if cached:
                data, remaining = cached
                return data, True, remaining, False

        cost = odds_call_cost()
        if not budget_allows(cost):
            stale = _cache_get_stale(cache_key)
            if stale is not None:
                return stale, True, 0, True
            raise BudgetExceeded(f"Modo ahorro: quedan menos de {MIN_REMAINING_CREDITS} créditos en la cuenta, esta liga no se consultó.")

        frm, to = today_window()
        params = {
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": "iso",
            "commenceTimeFrom": frm,
        }
        if to:
            params["commenceTimeTo"] = to
        if BOOKMAKERS:
            params["bookmakers"] = ",".join(BOOKMAKERS)
        else:
            params["regions"] = REGIONS
        logger.info("Fetching odds para %s (costo estimado %d)", sport_key, cost)
        data = _api_get(f"/sports/{sport_key}/odds/", params, counts_spend=True)
        _cache_set(cache_key, data)
        return data, False, CACHE_TTL, False

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
        book_key = b.get("key")
        for m in b.get("markets", []):
            if m.get("key") != mkey:
                continue
            for o in m.get("outcomes", []):
                rows.append({
                    "bookmaker": title,
                    "book_key": book_key,
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

# Prioridad de casas "sharp" (bajo vig, referencia de precio justo) para
# anclar el fair price en vez de promediar entre todas las casas
# consultadas. Pinnacle primero (estandar de la industria); LowVig.ag como
# proxy si Pinnacle no esta disponible. Pinnacle (region eu) llega via el
# parametro 'bookmakers' (ver BOOKMAKERS) sin pagar una segunda region; si
# un mercado no lo cotiza, el ancla es LowVig.ag, y si tampoco, consenso.
SHARP_BOOKS_PRIORITY = ["Pinnacle", "LowVig.ag"]

def sharp_or_consensus_fair(outcomes: list) -> tuple[dict, dict, dict]:
    """
    Fair price anclado a una casa sharp cuando esta disponible, en vez del
    promedio/consenso simple entre todas las casas (no_vig_h2h_probabilities).
    Para cada (name, point) que una casa de SHARP_BOOKS_PRIORITY cotiza con
    sus dos puntas, el de-vig se hace SOLO con esas dos puntas (el precio de
    una casa sharp ya es la referencia de mercado, promediarlo con casas mas
    blandas lo diluye). Si ninguna casa sharp cotiza ese mercado especifico,
    cae al fallback de consenso actual.

    'counts' (corroboracion entre casas, usado por EFFICIENCY_MIN_BOOKS) no
    cambia: sigue siendo el conteo de consenso completo, sea cual sea el
    origen del fair — la corroboracion es independiente de que precio se usa
    como ancla. 'source' documenta que criterio se uso por señal, ver
    get_dashboard()/edge_source_stats y Debug.
    """
    fair, counts = no_vig_h2h_probabilities(outcomes)
    source = {k: "consensus" for k in fair}

    by_sharp_book: dict[str, list] = {}
    for o in outcomes:
        book = o.get("bookmaker")
        if book not in SHARP_BOOKS_PRIORITY:
            continue
        by_sharp_book.setdefault(book, []).append(o)

    for book in SHARP_BOOKS_PRIORITY:
        rows = by_sharp_book.get(book)
        if not rows:
            continue
        groups: dict = {}
        for o in rows:
            name  = o.get("name")
            point = o.get("point")
            price = o.get("price")
            if name is None or price is None:
                continue
            imp = implied_probability_american(price)
            if imp is not None:
                groups.setdefault(point, []).append((name, imp))
        for point, items in groups.items():
            if len(items) < 2:
                continue
            total = sum(p for _, p in items)
            if total <= 0:
                continue
            for name, p in items:
                key = (name, point)
                if source.get(key, "consensus") != "consensus":
                    continue  # ya anclado por una casa sharp de mayor prioridad
                fair[key] = clamp(p / total, 0.01, 0.99)
                source[key] = f"sharp:{book}"

    return fair, counts, source

def is_bettable(o: dict) -> bool:
    """Solo casas de BETTABLE_BOOKS pueden ser la casa recomendada. Pinnacle
    y las demás casas sharp siguen entrando al precio justo y al conteo de
    corroboración, pero nunca como lugar donde apostar."""
    if not BETTABLE_BOOKS:
        return True
    return (o.get("book_key") or "").lower() in BETTABLE_BOOKS

def best_price_filtered(outcomes: list, name: str, fair: float, point=None) -> Optional[dict]:
    cand = []
    for o in outcomes:
        if o.get("name") != name:
            continue
        if not is_bettable(o):
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
# Kelly score — criterio único para Tickets/Parlays
# ---------------------------------------------------------------------------
KELLY_FRACTION    = 0.25   # Kelly fraccional (1/4): full Kelly apuesta demasiado agresivo
KELLY_SCORE_SCALE = 200.0  # escala kelly_usado (fraccion de bankroll) a un score 0-10
KELLY_MIN_SCORE   = 0.5    # piso del tier "Leve" (ver KELLY_TIERS/kelly_tier)
# Bajado de 2.0 a 0.5: kelly_full se clampea a 0 cuando no hay edge medible
# (max(p-(1-p)/b, 0)), y en 25 capturas reales de produccion eso pasa en el
# ~92% de los dias — ni bajando el piso a 0.1 se rescataban señales mas de
# 2/25 dias. El piso en si no era el cuello de botella (2.0 exige solo
# ~4pp de edge real con confianza plena de libros, razonable en terminos
# de Kelly), pero bajarlo a 0.5 al menos no descarta los dias "casi" que
# antes se perdian por decimas.

TICKET_MIN_SCORE  = 0.6    # piso para admitir una pata en Tickets/Parlays
# Mas estricto que KELLY_MIN_SCORE (0.5) a proposito: un parlay combina el
# riesgo de todas sus patas (si una falla, se pierde el ticket completo),
# asi que una pata que apenas justifica un pick individual suelto no
# deberia alcanzar para arrastrar a todo un combinado. Piso propio y
# separado del de Ranking Kelly — no comparten criterio de admision.
# Bajado de 1.5 a 0.6: verificado contra 33 capturas reales de produccion
# (13 ago - 4 sep 2026, 2054 señales), el kelly_score MAXIMO observado en
# todo el periodo fue 0.7 — 1.5 era un piso literalmente inalcanzable con
# datos reales (0 señales lo cumplieron en 19 dias), no solo estricto. 0.6
# queda justo debajo de ese techo historico (sigue siendo mas estricto que
# KELLY_MIN_SCORE) sin ser matematicamente imposible de alcanzar.

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
def efficiency_failure_reason(prob, val, ev, odds, odds_ok, books=None, books_ok=True) -> str:
    """
    Detalla contra qué umbral(es) del piso más bajo antes de EVITAR
    (EFFICIENCY_MIN_EV_BLUE, o el fallback de prob/val de PROBABLE) falló
    el pick, para diagnosticar sin tener que inspeccionar prob/val/ev
    manualmente en cada caso.
    """
    fails = []
    if ev < EFFICIENCY_MIN_EV_BLUE:
        fails.append(f"EV {ev:.1f}% <{EFFICIENCY_MIN_EV_BLUE:.1f}%")
    elif prob < PROBABLE_FALLBACK_MIN_PROB:
        fails.append(f"Prob {prob:.1f}% <{PROBABLE_FALLBACK_MIN_PROB:.0f}%")
    elif val < PROBABLE_FALLBACK_MIN_VAL:
        fails.append(f"Val {val:.1f} <{PROBABLE_FALLBACK_MIN_VAL:.0f}")
    if not odds_ok:
        fails.append(f"Odds {odds} peor que {EFFICIENCY_MAX_ODDS}")
    if not books_ok:
        fails.append(f"{books} casa(s) <{EFFICIENCY_MIN_BOOKS} — sin corroboracion suficiente")
    if not fails:
        return "No cumple criterios de eficiencia."
    return "Falla: " + " · ".join(fails)

def classify_efficiency(prob, val, ev, edge, odds, books=None, pick_id: Optional[str] = None) -> tuple[str, str, str, str]:
    ev    = ev or 0.0
    edge  = edge or 0.0
    odds_ok  = odds is None or odds >= EFFICIENCY_MAX_ODDS
    books_ok = books is not None and books >= EFFICIENCY_MIN_BOOKS

    # Histéresis del piso de SÓLIDO: sin esto, un pick justo en el borde
    # (EV/edge apenas sobre el piso) entra y sale de SÓLIDO con el ruido
    # normal de cuotas en vivo entre cada "Actualizar" (force_refresh trae
    # precios nuevos de verdad, no cacheados). Si el pick YA estaba SÓLIDO
    # en el scan anterior, se le exige caer mas de EFFICIENCY_GREEN_EXIT_*
    # por debajo del piso normal (no apenas cruzarlo) para bajarlo de tier.
    min_ev_green   = EFFICIENCY_MIN_EV_GREEN
    min_edge_green = EFFICIENCY_MIN_EDGE_GREEN
    if _prev_tier(pick_id) == "green":
        min_ev_green   -= EFFICIENCY_GREEN_EXIT_EV_BUFFER
        min_edge_green -= EFFICIENCY_GREEN_EXIT_EDGE_BUFFER

    if (ev >= min_ev_green and prob >= EFFICIENCY_MIN_PROB and val >= EFFICIENCY_MIN_VAL
            and edge >= min_edge_green and odds_ok and books_ok):
        _remember_tier(pick_id, "green")
        return "green", "🔒 SÓLIDO", "Favorito con alta probabilidad y buen valor.", "0.5u-1u"

    if (ev >= EFFICIENCY_MIN_EV_BLUE and prob >= 58 and val >= 60 and edge >= EFFICIENCY_MIN_EDGE_BLUE
            and odds_ok and books_ok):
        _remember_tier(pick_id, "blue")
        return "blue", "📌 PROBABLE", "Alta probabilidad pero precio ajustado.", "0.25u"

    # Ex-tier ESPECULATIVO (eliminado como categoria propia): estas señales
    # no llegan al gate normal de PROBABLE (prob>=58/val>=60/edge>=-1.5),
    # pero su EV real SI entra en el piso de PROBABLE (EFFICIENCY_MIN_EV_
    # BLUE) — el EV real es lo que las califica, no la probabilidad de
    # acierto. Se mantiene el piso mas bajo de prob/val (PROBABLE_FALLBACK_
    # MIN_*) para filtrar ruido, pero sin exigir el piso completo de
    # PROBABLE. Antes clasificaban en un tier "ESPECULATIVO" aparte con su
    # propio piso de EV mas laxo (-1.5); ahora usan el mismo piso que el
    # resto de PROBABLE (-1.0) — lo que caia entre -1.5 y -1.0 pasa a
    # EVITAR (verificado contra 33 capturas reales: 53 señales promovidas,
    # EV% promedio +0.66%; 21 correctamente a EVITAR, EV% promedio -1.31%).
    if (ev >= EFFICIENCY_MIN_EV_BLUE and prob >= PROBABLE_FALLBACK_MIN_PROB and val >= PROBABLE_FALLBACK_MIN_VAL
            and odds_ok and books_ok):
        _remember_tier(pick_id, "blue")
        return "blue", "📌 PROBABLE", "Probabilidad moderada con EV real dentro del piso — mayor riesgo que el gate normal.", "0.1u-0.25u"

    # Salvaguarda: un EV real positivo nunca debe terminar en EVITAR aunque
    # prob/val (bajos por diseño en un underdog) no lleguen al piso de
    # arriba — ese es exactamente el bug diagnosticado (underdog con EV
    # positivo marcado EVITAR). odds_ok/books_ok se mantienen: no son el
    # sesgo de Validación que se esta corrigiendo, son filtros de calidad de
    # dato (precio demasiado corto / poca corroboracion entre casas).
    #
    # v8.7.1: PROBABLE respeta el piso de probabilidad (PROBABLE_FALLBACK_
    # MIN_PROB). Antes esta salvaguarda devolvia PROBABLE sin mirar la
    # probabilidad, y en produccion llenaba PROBABLE de underdogs de +700 a
    # +2500 con 4-13% de probabilidad (18 de 19 PROBABLE el 24-nov). Un EV
    # positivo por debajo del piso es un underdog de cuota alta: etiqueta
    # propia (gold, "CUOTA ALTA"), que solo se muestra en Cuotas Altas
    # (ver high_odds_picks) y no entra a SOLIDO/PROBABLE/Mejores Jugadas.
    if ev > 0 and odds_ok and books_ok:
        if prob >= PROBABLE_FALLBACK_MIN_PROB:
            _remember_tier(pick_id, "blue")
            return "blue", "📌 PROBABLE", f"EV real positivo ({ev:.1f}%) pero no cumple el resto de las compuertas de calidad.", "0.1u"
        _remember_tier(pick_id, "gold")
        return "gold", "💰 CUOTA ALTA", f"Underdog con EV positivo ({ev:.1f}%) y probabilidad {prob:.1f}% bajo el piso de PROBABLE — solo para Cuotas Altas.", "0.1u"

    reason = efficiency_failure_reason(prob, val, ev, odds, odds_ok, books, books_ok)
    _remember_tier(pick_id, "red")
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
    # Cuota alta sin Pinnacle como ancla: el edge sale del consenso o de
    # LowVig y en underdogs grandes ese de-vig tiene ~1 pt de error, del
    # mismo tamaño que el edge medido (EV = edge x cuota lo infla). Se marca
    # en la UI y queda fuera de Mejores Jugadas y Cuotas Altas.
    sig["pinnacle_anchor"] = sig.get("fair_source") == "sharp:Pinnacle"
    sig["no_sharp_anchor"] = (sig.get("odds") or 0) >= HIGH_ODDS_MIN and not sig["pinnacle_anchor"]
    return sig

# ---------------------------------------------------------------------------
# Señales ML - EFICIENCIA
# ---------------------------------------------------------------------------
def moneyline_efficiency(game: dict, sport: str) -> list[dict]:
    outs = get_market_outcomes(game, "h2h")
    fair, counts, source = sharp_or_consensus_fair(outs)
    signals = []

    for (name, _point), p in fair.items():
        best = best_price_filtered(outs, name, p)
        if not best:
            continue
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        books = counts.get((name, _point), 0)
        val   = smooth_validation(prob, odds, books, ev, edge)
        pick_id = f"{sport}:{game.get('away_team')}@{game.get('home_team')}:{game.get('commence_time')}:ML:{name}"
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds, books, pick_id=pick_id)

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
            "book_count":        books,
            "is_primary":        True,
            "fanduel_odds":      fd_odds,
            "fanduel_available": fd_odds is not None,
            "fair_source":       source.get((name, _point), "consensus"),
        }))

    return sorted(signals, key=lambda x: (x["validation"], x["probability"]), reverse=True)

# ---------------------------------------------------------------------------
# Señales Spread y Total - EFICIENCIA
# ---------------------------------------------------------------------------
def spread_signals(game: dict, sport: str) -> list[dict]:
    outs = get_market_outcomes(game, "spreads")
    if not outs:
        return []
    fair, counts, source = sharp_or_consensus_fair(outs)

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
        books = counts.get((name, point), 0)
        val   = smooth_validation(prob, odds, books, ev, edge)
        pick_id = f"{sport}:{game.get('away_team')}@{game.get('home_team')}:{game.get('commence_time')}:SPR:{name}:{point}"
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds, books, pick_id=pick_id)
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
            "book_count":        books,
            "is_primary":        False,
            "is_alt_line":       is_alt,
            "fanduel_odds":      fd_odds,
            "fanduel_available": fd_odds is not None,
            "point":             point,
            "fair_source":       source.get((name, point), "consensus"),
        }))
    return sorted(signals, key=lambda x: (ranking_edge(x), x["validation"], x["probability"]), reverse=True)


def total_signals(game: dict, sport: str) -> list[dict]:
    outs = get_market_outcomes(game, "totals")
    if not outs:
        return []
    fair, counts, source = sharp_or_consensus_fair(outs)
    signals = []
    for (name, point), p in fair.items():
        best = best_price_filtered(outs, name, p, point=point)
        if not best:
            continue
        odds  = best["american_odds"]
        prob  = round(clamp(p * 100, *PROB_CLAMP), 1)
        ev, edge = safe_ev_edge(p, odds)
        books = counts.get((name, point), 0)
        val   = smooth_validation(prob, odds, books, ev, edge)
        pick_id = f"{sport}:{game.get('away_team')}@{game.get('home_team')}:{game.get('commence_time')}:TOT:{name}:{point}"
        color, label, reason, stake = classify_efficiency(prob, val, ev, edge, odds, books, pick_id=pick_id)
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
            "book_count":        books,
            "is_primary":        False,
            "fanduel_odds":      fd_odds,
            "fanduel_available": fd_odds is not None,
            "point":             point,
            "fair_source":       source.get((name, point), "consensus"),
        }))
    return sorted(signals, key=lambda x: (ranking_edge(x), x["validation"], x["probability"]), reverse=True)


# ---------------------------------------------------------------------------
# Predicción completa por juego (v8.5)
# ---------------------------------------------------------------------------
_COLOR_TIER_RANK = {"green": 3, "blue": 2, "yellow": 1, "red": 0}

# Señales "gold" (CUOTA ALTA, ver classify_efficiency) no compiten por ser
# EL pick de un partido en SOLIDO/PROBABLE/EVITAR ni en Mejores Jugadas:
# viven solo en Cuotas Altas.
_REPRESENTATIVE_EXCLUDED = {"gold"}

def best_signal_for_game(signals: list) -> Optional[dict]:
    """
    Un solo pick representativo por partido entre todos sus mercados (ML/
    Spread/Total): el de mayor jerarquia de color (verde > azul > rojo) y,
    dentro de la misma jerarquia, el de mejor ranking_edge. Antes ML/Spread/
    Total de un mismo partido se clasificaban y mostraban de forma
    independiente, así que el mismo partido podía aparecer en SÓLIDO por su
    ML y en PROBABLE por su Spread al mismo tiempo (bug diagnosticado en
    TODAS las secciones de clasificación). Esta función centraliza el
    criterio de "cuál es EL pick de este partido" para que best_market() y
    los buckets de get_dashboard() (SÓLIDO/PROBABLE/EVITAR) nunca puedan
    volver a divergir entre sí.
    """
    cands = [s for s in (signals or [])
             if s.get("ev") is not None and s.get("color") not in _REPRESENTATIVE_EXCLUDED]
    if not cands:
        return None
    best_tier = max(_COLOR_TIER_RANK.get(s.get("color"), 0) for s in cands)
    top = [s for s in cands if _COLOR_TIER_RANK.get(s.get("color"), 0) == best_tier]
    return max(top, key=ranking_edge)

def best_market(ml: list, spread: list, total: list) -> Optional[dict]:
    """
    Pick representativo del partido (ver best_signal_for_game) para el
    resumen ejecutivo "Mejor Pick del Día": solo se muestra si ese pick
    llega a color green/blue, mismas compuertas que SÓLIDO/PROBABLE. Si el
    mejor mercado del partido no pasa esas compuertas, no hay recomendación
    (None) en vez de mostrar el menos malo con etiqueta EVITAR contradictoria.
    """
    best = best_signal_for_game((ml or []) + (spread or []) + (total or []))
    if not best or best.get("color") not in ("green", "blue"):
        return None
    market_name = best.get("market")
    return {
        **best,
        "recommended_market": market_name,
        "reason": f"{market_name} — Val {best.get('validation')}% / EV {best.get('ev')}%",
        # Expuesto para que el front ordene la lista de juegos con el mismo
        # criterio que se usó acá adentro para elegir el mercado (antes
        # ordenaba por bm.ev crudo, reintroduciendo el sesgo en el cliente
        # aunque el backend ya eligiera bien). Ver ranking_edge.
        "rank_score": ranking_edge(best),
    }


def top_plays(game_best: list, n: int = TOP_PLAYS_COUNT) -> list:
    """
    "Mejores Jugadas del Día": las n mejores de todas las ligas juntas, un
    pick por partido (game_best ya viene de best_signal_for_game). Orden:
    jerarquía de color (SÓLIDO > PROBABLE > EVITAR) y dentro de cada una
    ranking_edge — el mismo criterio que elige el pick de cada partido.
    Si no hay n picks verdes/azules se completa con los mejores EVITAR (con
    su etiqueta visible), así la sección solo queda vacía si no hay juegos.
    No reclasifica nada: color/label son los de classify_efficiency.
    """
    # Nunca cuotas mayores a TOP_PLAYS_MAX_ODDS (+500) ni cuotas altas sin
    # Pinnacle como ancla (no_sharp_anchor): son las que inflan el EV.
    eligible = [
        s for s in game_best
        if (s.get("odds") or 0) <= TOP_PLAYS_MAX_ODDS and not s.get("no_sharp_anchor")
    ]
    ranked = sorted(
        eligible,
        key=lambda s: (_COLOR_TIER_RANK.get(s.get("color"), 0), ranking_edge(s)),
        reverse=True,
    )
    return [{**s, "top_rank": i + 1} for i, s in enumerate(ranked[:n])]

def high_odds_picks(signals: list) -> list:
    """
    "Cuotas Altas": underdogs entre +HIGH_ODDS_MIN y +HIGH_ODDS_MAX con EV
    positivo medido contra el precio justo de Pinnacle (fair_source ==
    'sharp:Pinnacle', no consenso ni LowVig). La casa recomendada ya es
    siempre una de BETTABLE_BOOKS (best_price_filtered). Agrega la ganancia
    potencial y el EV en dólares por cada $10 apostados. Es una vista: no
    toca color/tier ni entra a Tickets.
    """
    out = []
    for s in signals:
        odds, ev, dec = s.get("odds"), s.get("ev"), s.get("decimal_odds")
        if odds is None or ev is None or dec is None:
            continue
        if not (HIGH_ODDS_MIN <= odds <= HIGH_ODDS_MAX) or ev <= 0:
            continue
        if s.get("fair_source") != "sharp:Pinnacle":
            continue
        # Edge minimo en puntos de probabilidad: edges de 0.1-0.5 pts estan
        # dentro del error del de-vig en underdogs y no son señal.
        if (s.get("edge") or 0) < HIGH_ODDS_MIN_EDGE:
            continue
        if (s.get("book_count") or 0) < EFFICIENCY_MIN_BOOKS:
            continue
        fair_prob = s.get("probability")
        implied = implied_probability_american(odds)
        out.append({
            **s,
            "profit_per_10":   round(10 * (dec - 1), 2),
            "ev_per_10":       round(ev / 10, 2),
            "pinnacle_prob":   fair_prob,
            "implied_prob":    round(implied * 100, 1) if implied is not None else None,
        })
    out.sort(key=lambda x: x.get("ev") or 0, reverse=True)
    return out[:HIGH_ODDS_LIMIT]

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

def ticket_ok_market(sig: Optional[dict]) -> bool:
    """Piso minimo para parlays que no filtran por color (Mixto, ML+Total):
    NO exige green/blue (siguen mostrando todo el mercado, no solo picks
    'curados'). Usa TICKET_MIN_SCORE, mas estricto que el piso de Ranking
    Kelly a proposito: una pata que combinada en un parlay arrastra el
    riesgo de todo el ticket necesita mas margen que un pick individual
    suelto."""
    if not sig:
        return False
    return (sig.get("kelly_score") or 0.0) >= TICKET_MIN_SCORE

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
    if real_count < 2:
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
    kelly_scores = []
    for x in picks:
        comb *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg  += x.get("validation", 0)
        kelly_scores.append(x.get("kelly_score") or 0.0)
    avg /= real_count
    min_kelly = min(kelly_scores)
    avg_kelly = sum(kelly_scores) / real_count

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
        "min_kelly_score":      round(min_kelly, 1),
        "avg_kelly_score":      round(avg_kelly, 1),
        "combined_tier":        kelly_tier(min_kelly)[0],
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
    supera TICKET_MIN_SCORE: mismo piso que Parlay Mixto, mas estricto que
    Ranking Kelly porque acá el riesgo de las patas se combina.
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
        if max((l.get("kelly_score") or 0.0) for l in legs) >= TICKET_MIN_SCORE
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
    kelly_scores = []
    for x in picks:
        comb *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg  += x.get("validation", 0)
        kelly_scores.append(x.get("kelly_score") or 0.0)
    avg /= legs_count
    min_kelly = min(kelly_scores)
    avg_kelly = sum(kelly_scores) / legs_count

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
        "min_kelly_score":      round(min_kelly, 1),
        "avg_kelly_score":      round(avg_kelly, 1),
        "combined_tier":        kelly_tier(min_kelly)[0],
    }

# ---------------------------------------------------------------------------
# Dashboard principal v8.4
# ---------------------------------------------------------------------------
def get_dashboard(selected_sports: list, force_refresh: bool = False) -> dict:
    dash: dict = {
        "mode":             "Pro v8.7 - Multi-liga",
        "cache_ttl_seconds": CACHE_TTL,
        "refresh_interval_min": REFRESH_INTERVAL_MIN,

        # Mejores Jugadas del Día (top N de todas las ligas) y Cuotas Altas
        # (underdogs +EV contra Pinnacle) — ver top_plays / high_odds_picks.
        "top_plays":  [],
        "high_odds":  [],
        "high_odds_range": [HIGH_ODDS_MIN, HIGH_ODDS_MAX],
        "high_odds_min_edge": HIGH_ODDS_MIN_EDGE,

        # Contador de créditos de The Odds API (headers x-requests-*).
        "api_usage": {},
        "only_today":       ONLY_TODAY,
        "timezone":         LOCAL_TZ,
        "sports":           {},
        "all_games":        [],

        # Todos los Juegos — cartelera del día sin predicción (equipos,
        # horario, liga). Version liviana de all_games, sin ml/spread/total.
        "schedule": [],

        # Apuestas EV (MLB/NBA/NHL) — TODAS las señales del día (ML/Spread/
        # Total, todos los partidos), coloreadas según su clasificación real
        # (classify_efficiency decide el color, no la visibilidad). Estas
        # listas nunca se filtran ni se vacían por "no hay SÓLIDO/PROBABLE
        # suficiente": siempre muestran el mercado completo, por EV.
        "efficiency_green":  [],
        "efficiency_blue":   [],
        "avoid": [],

        # Tickets
        "ticket_efficiency": None,

        # Documenta que criterio de fair price se uso por señal (ancla sharp
        # vs consenso), agregado sobre todas las señales del día — ver
        # sharp_or_consensus_fair(). Visible en Debug.
        "edge_source_stats": {},

        "warnings": [],
    }

    # Ligas en temporada (/sports, gratis). None = no se pudo leer: no se
    # descarta ninguna y se sigue con /events.
    active_keys = fetch_active_sport_keys() if API_KEY else None
    budget_blocked: list[str] = []

    for label, key in SPORTS_EFFICIENCY.items():
        if label not in selected_sports and "ALL" not in selected_sports:
            continue
        if active_keys is not None and key not in active_keys:
            dash["sports"][label] = {
                "ok": True, "sport_key": key, "skipped": "Fuera de temporada",
                "games_count": 0, "total_api_games": 0,
            }
            continue
        try:
            # /events (gratis): si la liga no tiene partidos por empezar hoy
            # no se piden cuotas. Si /events falla (None) se piden igual.
            upcoming = fetch_upcoming_events(key) if API_KEY else None
            if upcoming is not None and not upcoming:
                dash["sports"][label] = {
                    "ok": True, "sport_key": key, "skipped": "Sin juegos por empezar hoy",
                    "games_count": 0, "total_api_games": 0,
                }
                continue
            games, from_cache, ttl, stale = fetch_odds(key, force_refresh=force_refresh)
            total = len(games)
            games = [g for g in games if is_game_today(g) and is_upcoming(g)]
            dash["sports"][label] = {
                "ok": True, "sport_key": key,
                "games_count": len(games), "total_api_games": total,
                "from_cache": from_cache, "cache_seconds_left": ttl,
                "stale": stale,
            }
            if stale:
                budget_blocked.append(f"{label} (cuotas guardadas)")
            for g in games:
                dash["all_games"].append(game_prediction_full(g, label))
                dash["schedule"].append({
                    "sport":      label,
                    "game":       f"{g.get('away_team')} vs {g.get('home_team')}",
                    "home_team":  g.get("home_team"),
                    "away_team":  g.get("away_team"),
                    "start_time": g.get("commence_time"),
                })
        except BudgetExceeded:
            # No es un error de la API: el guardián de presupuesto no dejó
            # consultarla (y no había cuotas guardadas para mostrar).
            logger.info("Liga %s no consultada por cupo de créditos", label)
            dash["sports"][label] = {
                "ok": True, "sport_key": key, "budget_blocked": True,
                "skipped": "⏸ Cupo de créditos — no consultada",
                "games_count": 0, "total_api_games": 0,
            }
            budget_blocked.append(f"{label} (sin consultar)")
        except Exception as e:
            logger.error("Error %s: %s", label, e, exc_info=True)
            dash["sports"][label] = {"ok": False, "sport_key": key, "error": str(e)}
            dash["warnings"].append(f"{label}: {e}")

    if budget_blocked:
        dash["warnings"].append(
            "⏸ Modo ahorro de créditos: " + ", ".join(budget_blocked) + ". " + budget_reason()
        )

    sorter_ev   = lambda x: x.get("ev") if x.get("ev") is not None else -999
    # Para listas/pools SIN piso de probabilidad (avoid, parlays sin color):
    # ranking_edge en vez de 'ev' crudo, para que una cuota alta no empuje
    # arriba un pick solo por el multiplicador de pago (ver ranking_edge).
    # SÓLIDO/PROBABLE quedan con sorter_ev: ya estan protegidos por el piso
    # de probabilidad de classify_efficiency, ahi 'ev' no tiene el sesgo.
    sorter_rank = lambda x: ranking_edge(x)
    # Tickets/Parlays comparten este criterio (ver kelly_score): el mismo
    # número decide qué patas entran y en qué orden en los parlays.
    sorter_kelly = lambda x: x.get("kelly_score") or 0.0

    # Todas las señales del día en un solo pool — base de Apuestas Edge/EV y
    # Parlays. No se colapsa a "1 mejor pick por juego": cada mercado de
    # cada partido aparece con su color real.
    all_signals = []
    for full in dash["all_games"]:
        for mkey in ("ml", "spread", "total"):
            for sig in full.get(mkey, []):
                all_signals.append({**sig, "game": full["game"], "start_time": full["start_time"]})
    all_signals = dedupe(all_signals)

    dash["edge_source_stats"] = dict(Counter(s.get("fair_source", "consensus") for s in all_signals))

    # Un solo pick por partido en SÓLIDO/PROBABLE/EVITAR (ver
    # best_signal_for_game): antes cada mercado (ML/Spread/Total) de cada
    # partido se clasificaba y listaba por separado, así que un mismo
    # partido podía aparecer repartido entre dos o más de estas secciones a
    # la vez. Solo estas listas se consolidan — Tickets/Parlays y "Apuestas
    # Edge" siguen usando all_signals/all_games sin agrupar, tal como antes.
    signals_by_game: dict = {}
    for s in all_signals:
        signals_by_game.setdefault(s.get("game"), []).append(s)
    game_best = [best_signal_for_game(sigs) for sigs in signals_by_game.values()]
    game_best = [s for s in game_best if s]

    dash["efficiency_green"]  = sorted([s for s in game_best if s.get("color") == "green"],  key=sorter_ev, reverse=True)
    dash["efficiency_blue"]   = sorted([s for s in game_best if s.get("color") == "blue"],   key=sorter_ev, reverse=True)
    dash["avoid"]             = sorted([s for s in game_best if s.get("color") == "red"],    key=sorter_rank, reverse=True)

    dash["top_plays"] = top_plays(game_best)
    dash["high_odds"] = high_odds_picks(all_signals)

    # Ticket Eficiencia: producto curado (requiere green/blue), queda
    # deshabilitado en la UI (display:none) pero se mantiene funcional.
    eff_pool = dedupe(dash["efficiency_green"] + dash["efficiency_blue"])
    dash["ticket_efficiency"] = build_ticket(
        "🔒 Ticket Eficiencia", eff_pool, 6, ticket_ok_efficiency,
        "green", "Bajo", "Picks de alta probabilidad en todas las ligas."
    )

    # Parlay Mixto (3/6/10 legs) — ML + Spread + Total, top Score Kelly del
    # día sin filtrar por color (ticket_ok_market usa TICKET_MIN_SCORE, mas
    # estricto que el de Ranking Kelly porque acá el riesgo de las patas se
    # combina; no exige green/blue: sigue mostrando todo el mercado que
    # califica). Ordenado por kelly_score. Solo devuelve None si
    # literalmente no hay 3 patas que superen el piso hoy (build_ticket ya
    # lo garantiza). El tope por mercado evita que ML monopolice las patas
    # cuando hay Spread/Total disponibles.
    _parlay_pool = sorted(all_signals, key=sorter_kelly, reverse=True)
    dash["ticket_3"]  = build_ticket("🎯 Parlay Mixto — 3 Legs",  _parlay_pool, 3,  ticket_ok_market, "yellow", "Bajo",  "3 mejores picks del día por Score Kelly (ML/Spread/Total).", max_per_market=_market_cap(3))
    dash["ticket_6"]  = build_ticket("🔥 Parlay Mixto — 6 Legs",  _parlay_pool, 6,  ticket_ok_market, "yellow", "Medio", "6 mejores picks del día por Score Kelly (ML/Spread/Total).", max_per_market=_market_cap(6))
    dash["ticket_10"] = build_ticket("⭐ Parlay Mixto — 10 Legs", _parlay_pool, 10, ticket_ok_market, "yellow", "Alto",  "10 mejores picks del día por Score Kelly (ML/Spread/Total).", max_per_market=_market_cap(10))

    # Parlay ML + Total (3/6/10 legs) — excluye Spread. Elige PARTIDOS por
    # el mejor Score Kelly combinado (ML+Total de ese partido, ver
    # build_mltotal_ticket), no patas sueltas por EV individual: así
    # siempre entran ambas patas del partido elegido, y solo entran
    # partidos donde al menos una pata supera TICKET_MIN_SCORE (mismo piso
    # que Parlay Mixto).
    _parlay_pool_mltotal = [s for s in all_signals if s.get("market") != "Spread"]
    dash["ticket_mltotal_3"]  = build_mltotal_ticket("🎯 Parlay ML+Total — 3 Juegos",  _parlay_pool_mltotal, 3,  "yellow", "Bajo",  "3 mejores juegos del día por Score Kelly combinado (ML+Total).")
    dash["ticket_mltotal_6"]  = build_mltotal_ticket("🔥 Parlay ML+Total — 6 Juegos",  _parlay_pool_mltotal, 6,  "yellow", "Medio", "6 mejores juegos del día por Score Kelly combinado (ML+Total).")
    dash["ticket_mltotal_10"] = build_mltotal_ticket("⭐ Parlay ML+Total — 10 Juegos", _parlay_pool_mltotal, 10, "yellow", "Alto",  "10 mejores juegos del día por Score Kelly combinado (ML+Total).")

    usage = usage_status()
    dash["api_usage"] = usage
    if usage.get("low"):
        dash["warnings"].append(
            f"Quedan {usage['remaining']:,} créditos de The Odds API ({usage['pct_remaining']}%) — menos del {int(LOW_CREDITS_PCT)}% del plan."
        )
    return dash

# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
def debug_all_sports() -> dict:
    """Estado de ligas usando solo /sports y /events (no gastan créditos):
    antes llamaba fetch_odds por liga, que con 13 ligas y la caché fría
    podía costar ~39 créditos por cada vista de Debug."""
    res = {
        "api_key": api_key_status(),
        "regions": REGIONS, "markets": MARKETS,
        "bookmakers": BOOKMAKERS, "bettable_books": sorted(BETTABLE_BOOKS),
        "cache_ttl_seconds": CACHE_TTL,
        "only_today": ONLY_TODAY, "timezone": LOCAL_TZ,
        "api_usage": usage_status(),
        "sports": {},
    }
    active_keys = fetch_active_sport_keys() if API_KEY else None
    for label, key in SPORTS.items():
        if active_keys is not None and key not in active_keys:
            res["sports"][label] = {"ok": True, "sport_key": key, "skipped": "Fuera de temporada"}
            continue
        upcoming = fetch_upcoming_events(key) if API_KEY else None
        if upcoming is None:
            res["sports"][label] = {"ok": False, "sport_key": key, "error": "No se pudo leer /events"}
        else:
            res["sports"][label] = {"ok": True, "sport_key": key, "upcoming_today": len(upcoming)}
    return res
