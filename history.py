"""
history.py — Historial de picks (v8.8).

Guarda cada pick recomendado la primera vez que aparece (Mejores Jugadas,
SÓLIDO, PROBABLE y Cuotas Altas) con la cuota y el EV de ese momento, y
después lo califica con el marcador real de GET /scores de The Odds API.

Almacenamiento:
  - DATABASE_URL (Postgres, p. ej. Neon gratis): sobrevive deploys de Render.
  - Sin DATABASE_URL: SQLite en HISTORY_DB (history.db). Sirve en local; en
    Render el disco es efímero y se borra en cada deploy/reinicio.

Créditos (la cuenta puede tener solo 500/mes):
  - Guardar picks: 0 créditos (usa las cuotas que ya se pidieron).
  - Calificar: GET /scores?daysFrom=3 = 2 créditos por llamada. Una llamada
    por liga cada SCORES_INTERVAL_H (48 h), solo ligas con picks pendientes
    cuyo partido empezó hace más de GRADE_DELAY_H, y nunca con menos de
    HISTORY_MIN_REMAINING (100) créditos en la cuenta.
  - CLV (apagado por defecto, CLV_ENABLED=false): 1 crédito por cada
    partido+mercado con picks, pedido una vez dentro de los CLV_WINDOW_MIN
    previos al inicio (solo Pinnacle, 1 mercado).

Nada de esto puede romper el dashboard: todo corre en un hilo aparte y los
errores solo se loguean.
"""

import os
import sqlite3
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Callable, Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("history")

DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
HISTORY_DB: str = os.getenv("HISTORY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db"))
HISTORY_ENABLED: bool = os.getenv("HISTORY_ENABLED", "1") == "1"
LOCAL_TZ: str = os.getenv("LOCAL_TZ", "America/New_York")

# Calificación con /scores
SCORES_INTERVAL_H: float = float(os.getenv("SCORES_INTERVAL_H", "48"))
SCORES_DAYS_FROM: int = 3          # máximo que acepta /scores
SCORES_COST: int = 2               # /scores con daysFrom cuesta 2 créditos
HISTORY_MIN_REMAINING: int = int(os.getenv("HISTORY_MIN_REMAINING", "100"))
GRADE_DELAY_H: float = float(os.getenv("GRADE_DELAY_H", "4"))
# Si /scores falla, se reintenta en estas horas (no en cada carga).
SCORES_RETRY_H: float = 6.0

# CLV (Closing Line Value) — apagado por defecto.
CLV_ENABLED: bool = os.getenv("CLV_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
CLV_WINDOW_MIN: int = int(os.getenv("CLV_WINDOW_MIN", "60"))
CLV_COST: int = 1                  # 1 mercado x 1 casa (Pinnacle)

# Cada cuánto se revisa la base buscando picks para calificar/CLV.
CHECK_EVERY_S: int = 15 * 60
# Tras un error de conexión no se vuelve a intentar durante este tiempo
# (Neon caído no debe frenar cada carga).
DB_BACKOFF_S: int = 5 * 60

MARKET_API_KEY = {"ML": "h2h", "SPR": "spreads", "TOT": "totals"}

_PG = bool(DATABASE_URL)
_GREATEST = "GREATEST" if _PG else "MAX"

# ---------------------------------------------------------------------------
# Base de datos (Postgres o SQLite con el mismo SQL)
# ---------------------------------------------------------------------------
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS picks (
        id                TEXT PRIMARY KEY,
        created_at        TEXT NOT NULL,
        pick_date         TEXT NOT NULL,
        sport             TEXT NOT NULL,
        sport_key         TEXT NOT NULL,
        event_id          TEXT,
        game              TEXT,
        home_team         TEXT,
        away_team         TEXT,
        start_time        TEXT NOT NULL,
        market            TEXT NOT NULL,
        outcome           TEXT NOT NULL,
        point             DOUBLE PRECISION,
        selection         TEXT,
        odds              INTEGER,
        decimal_odds      DOUBLE PRECISION,
        bookmaker         TEXT,
        probability       DOUBLE PRECISION,
        ev                DOUBLE PRECISION,
        edge              DOUBLE PRECISION,
        color             TEXT,
        label             TEXT,
        fair_source       TEXT,
        is_top            INTEGER NOT NULL DEFAULT 0,
        is_eff            INTEGER NOT NULL DEFAULT 0,
        is_high           INTEGER NOT NULL DEFAULT 0,
        status            TEXT NOT NULL DEFAULT 'pending',
        home_score        DOUBLE PRECISION,
        away_score        DOUBLE PRECISION,
        profit            DOUBLE PRECISION,
        graded_at         TEXT,
        closing_odds      INTEGER,
        closing_fair_prob DOUBLE PRECISION,
        clv               DOUBLE PRECISION,
        closing_at        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS picks_status_idx ON picks (status, sport_key)",
    "CREATE INDEX IF NOT EXISTS picks_date_idx ON picks (pick_date)",
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
]

_db_lock = threading.Lock()
_schema_ready = False
_db_down_until = 0.0


def backend_name() -> str:
    return "postgres" if _PG else "sqlite"


def _q(sql: str) -> str:
    return sql.replace("?", "%s") if _PG else sql


class _DB:
    def __enter__(self):
        global _db_down_until
        try:
            if _PG:
                import psycopg
                from psycopg.rows import dict_row
                # prepare_threshold=None: sin prepared statements, así funciona
                # también con la URL "pooled" de Neon (PgBouncer).
                self.c = psycopg.connect(DATABASE_URL, connect_timeout=10, row_factory=dict_row,
                                         prepare_threshold=None)
            else:
                self.c = sqlite3.connect(HISTORY_DB, timeout=10)
                self.c.row_factory = sqlite3.Row
        except Exception:
            _db_down_until = time.time() + DB_BACKOFF_S
            raise
        return self

    def __exit__(self, et, e, tb):
        try:
            if et is None:
                self.c.commit()
            else:
                self.c.rollback()
        finally:
            self.c.close()
        return False

    def exec(self, sql: str, params: tuple = ()):
        cur = self.c.cursor()
        cur.execute(_q(sql), params)
        return cur

    def many(self, sql: str, seq: list) -> None:
        if seq:
            self.c.cursor().executemany(_q(sql), seq)

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.exec(sql, params).fetchall()]


def _db() -> _DB:
    """Conexión lista para usar (crea las tablas la primera vez). Lanza
    RuntimeError durante el backoff tras un error de conexión."""
    global _schema_ready
    if time.time() < _db_down_until:
        raise RuntimeError("Base de historial no disponible (reintento en unos minutos)")
    if not _schema_ready:
        with _db_lock:
            if not _schema_ready:
                with _DB() as db:
                    for stmt in _SCHEMA:
                        db.exec(stmt)
                _schema_ready = True
    return _DB()


def _meta_get(db: _DB, k: str) -> Optional[str]:
    rows = db.all("SELECT v FROM meta WHERE k = ?", (k,))
    return rows[0]["v"] if rows else None


def _meta_set(db: _DB, k: str, v: str) -> None:
    db.exec("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, v))

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_date(start_time: str) -> str:
    dt = _parse_iso(start_time)
    if not dt:
        return _now().astimezone(ZoneInfo(LOCAL_TZ)).date().isoformat()
    return dt.astimezone(ZoneInfo(LOCAL_TZ)).date().isoformat()


def _american_to_decimal(odds) -> Optional[float]:
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o >= 100:
        return 1 + o / 100
    if o <= -100:
        return 1 + 100 / abs(o)
    return None


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# Registro de picks
# ---------------------------------------------------------------------------
# Flags ya escritos por pick (en memoria): evita reescribir la base en cada
# carga del dashboard si nada cambió.
_seen_flags: dict[str, tuple[int, int, int]] = {}


def pick_id(sport_key: str, sig: dict) -> str:
    """Un pick = partido + mercado + lado. La línea (point) no forma parte
    del id: si el spread se mueve de -3.5 a -4 sigue siendo el mismo pick y
    queda la cuota/línea de la primera vez que apareció."""
    ev_id = sig.get("event_id") or f"{sig.get('game')}@{sig.get('start_time')}"
    return f"{sport_key}|{ev_id}|{sig.get('short_market')}|{sig.get('outcome')}"


def _collect(dash: dict, include_top: bool, sport_keys: dict) -> dict[str, dict]:
    rows: dict[str, dict] = {}

    def add(sig: dict, flag: str) -> None:
        sk = sport_keys.get(sig.get("sport"))
        if not sk or not sig.get("start_time") or not sig.get("outcome") or sig.get("short_market") not in MARKET_API_KEY:
            return
        pid = pick_id(sk, sig)
        row = rows.get(pid)
        if row is None:
            row = rows[pid] = {"sig": sig, "sport_key": sk, "is_top": 0, "is_eff": 0, "is_high": 0}
        row[flag] = 1

    if include_top:
        # Mejores Jugadas completa con EVITAR cuando faltan picks: esos se
        # muestran como no jugables y no entran al historial.
        for s in dash.get("top_plays") or []:
            if s.get("color") in ("green", "blue"):
                add(s, "is_top")
    for s in (dash.get("efficiency_green") or []) + (dash.get("efficiency_blue") or []):
        add(s, "is_eff")
    for s in dash.get("high_odds") or []:
        add(s, "is_high")
    return rows


def record(dash: dict, include_top: bool, sport_keys: dict) -> int:
    """Inserta los picks nuevos y suma flags a los existentes. Devuelve
    cuántas filas se escribieron."""
    rows = _collect(dash, include_top, sport_keys)
    todo = []
    for pid, r in rows.items():
        flags = (r["is_top"], r["is_eff"], r["is_high"])
        prev = _seen_flags.get(pid)
        if prev and all(p >= f for p, f in zip(prev, flags)):
            continue
        todo.append((pid, r, flags))
    if not todo:
        return 0

    now = _iso(_now())
    params = []
    for pid, r, (top, eff, high) in todo:
        s = r["sig"]
        params.append((
            pid, now, _local_date(s["start_time"]), s.get("sport"), r["sport_key"], s.get("event_id"),
            s.get("game"), s.get("home_team"), s.get("away_team"), s["start_time"],
            s.get("short_market"), s.get("outcome"), _num(s.get("point")), s.get("selection"),
            s.get("odds"), _num(s.get("decimal_odds")), s.get("bookmaker"),
            _num(s.get("probability")), _num(s.get("ev")), _num(s.get("edge")),
            s.get("color"), s.get("label"), s.get("fair_source"),
            top, eff, high,
        ))
    sql = f"""
        INSERT INTO picks (id, created_at, pick_date, sport, sport_key, event_id,
            game, home_team, away_team, start_time,
            market, outcome, point, selection,
            odds, decimal_odds, bookmaker,
            probability, ev, edge,
            color, label, fair_source,
            is_top, is_eff, is_high)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            is_top  = {_GREATEST}(picks.is_top,  excluded.is_top),
            is_eff  = {_GREATEST}(picks.is_eff,  excluded.is_eff),
            is_high = {_GREATEST}(picks.is_high, excluded.is_high)
    """
    with _db() as db:
        db.many(sql, params)
    for pid, _r, flags in todo:
        prev = _seen_flags.get(pid, (0, 0, 0))
        _seen_flags[pid] = tuple(max(p, f) for p, f in zip(prev, flags))
    return len(todo)

# ---------------------------------------------------------------------------
# Calificación con /scores
# ---------------------------------------------------------------------------
def grade_pick(p: dict, event: dict) -> Optional[tuple[str, float, float, float]]:
    """(status, profit por unidad, home_score, away_score) o None si el
    marcador no permite calificar."""
    home, away = event.get("home_team"), event.get("away_team")
    scores = {s.get("name"): _num(s.get("score")) for s in (event.get("scores") or [])}
    hs, as_ = scores.get(home), scores.get(away)
    if hs is None or as_ is None:
        return None
    market, outcome, point = p.get("market"), p.get("outcome"), p.get("point")

    def side() -> Optional[tuple[float, float]]:
        if outcome == home:
            return hs, as_
        if outcome == away:
            return as_, hs
        return None

    if market == "ML":
        if outcome == "Draw":
            res = "won" if hs == as_ else "lost"
        else:
            sd = side()
            if sd is None:
                return None
            mine, other = sd
            if mine > other:
                res = "won"
            elif mine < other:
                res = "lost"
            else:
                # Fútbol a 3 vías: el empate pierde la apuesta a un equipo.
                res = "lost" if (p.get("sport_key") or "").startswith("soccer") else "push"
    elif market == "SPR":
        sd = side()
        if sd is None or point is None:
            return None
        diff = sd[0] + float(point) - sd[1]
        res = "won" if diff > 0 else "lost" if diff < 0 else "push"
    elif market == "TOT":
        if point is None or outcome not in ("Over", "Under"):
            return None
        diff = (hs + as_) - float(point)
        if diff == 0:
            res = "push"
        elif (diff > 0) == (outcome == "Over"):
            res = "won"
        else:
            res = "lost"
    else:
        return None

    dec = p.get("decimal_odds") or _american_to_decimal(p.get("odds"))
    if res == "won":
        profit = round((dec or 1) - 1, 4)
    elif res == "lost":
        profit = -1.0
    else:
        profit = 0.0
    return res, profit, hs, as_


def _match_event(p: dict, by_id: dict, events: list) -> Optional[dict]:
    if p.get("event_id") and p["event_id"] in by_id:
        return by_id[p["event_id"]]
    for e in events:
        if (e.get("home_team") == p.get("home_team") and e.get("away_team") == p.get("away_team")
                and (e.get("commence_time") or "")[:10] == (p.get("start_time") or "")[:10]):
            return e
    return None


def apply_scores(db: _DB, sport_key: str, events: list) -> dict:
    by_id = {e.get("id"): e for e in events if e.get("id")}
    pending = db.all("SELECT * FROM picks WHERE status = 'pending' AND sport_key = ?", (sport_key,))
    now = _now()
    too_old = _iso(now - timedelta(days=SCORES_DAYS_FROM))
    out = {"graded": 0, "no_result": 0}
    for p in pending:
        ev = _match_event(p, by_id, events)
        g = grade_pick(p, ev) if ev and ev.get("completed") else None
        if g:
            status, profit, hs, as_ = g
            db.exec("UPDATE picks SET status = ?, profit = ?, home_score = ?, away_score = ?, graded_at = ? WHERE id = ?",
                    (status, profit, hs, as_, _iso(now), p["id"]))
            out["graded"] += 1
        elif (p.get("start_time") or "") < too_old:
            # Fuera de la ventana de /scores (3 días): ya no se puede calificar
            # (partido suspendido/cancelado o sin llamadas a tiempo).
            db.exec("UPDATE picks SET status = 'no_result', graded_at = ? WHERE id = ?", (_iso(now), p["id"]))
            out["no_result"] += 1
    return out


def maybe_grade(fetch_scores: Callable[[str], list], remaining: Callable[[], Optional[int]]) -> None:
    now = _now()
    cutoff = _iso(now - timedelta(hours=GRADE_DELAY_H))
    with _db() as db:
        leagues = db.all("SELECT DISTINCT sport_key FROM picks WHERE status = 'pending' AND start_time <= ?", (cutoff,))
        for row in leagues:
            sk = row["sport_key"]
            last = _parse_iso(_meta_get(db, f"scores_next:{sk}"))
            if last and now < last:
                continue
            rem = remaining()
            if rem is None or rem < HISTORY_MIN_REMAINING:
                logger.info("Historial: /scores de %s salteado (créditos restantes: %s, mínimo %d)",
                            sk, rem, HISTORY_MIN_REMAINING)
                return
            try:
                events = fetch_scores(sk)
            except Exception as e:
                logger.warning("Historial: /scores de %s falló: %s", sk, e)
                _meta_set(db, f"scores_next:{sk}", _iso(now + timedelta(hours=SCORES_RETRY_H)))
                continue
            _meta_set(db, f"scores_last:{sk}", _iso(now))
            _meta_set(db, f"scores_next:{sk}", _iso(now + timedelta(hours=SCORES_INTERVAL_H)))
            res = apply_scores(db, sk, events or [])
            logger.info("Historial: %s calificados=%d sin_resultado=%d", sk, res["graded"], res["no_result"])

# ---------------------------------------------------------------------------
# CLV (apagado por defecto)
# ---------------------------------------------------------------------------
def closing_fair(p: dict, event_odds: dict) -> tuple[Optional[int], Optional[float]]:
    """(cuota de Pinnacle para el lado del pick, prob. justa sin vig) en el
    momento del cierre. None si Pinnacle no cotiza esa misma línea."""
    mkey = MARKET_API_KEY.get(p.get("market"))
    for bm in event_odds.get("bookmakers") or []:
        if bm.get("key") != "pinnacle":
            continue
        for m in bm.get("markets") or []:
            if m.get("key") != mkey:
                continue
            outs = m.get("outcomes") or []
            pt = p.get("point")
            if mkey == "h2h":
                group = outs
            elif mkey == "totals":
                group = [o for o in outs if _num(o.get("point")) == pt]
            else:  # spreads: el lado del pick con su línea y el rival con la opuesta
                group = [o for o in outs
                         if (o.get("name") == p.get("outcome") and _num(o.get("point")) == pt)
                         or (o.get("name") != p.get("outcome") and pt is not None and _num(o.get("point")) == -pt)]
            mine = [o for o in group if o.get("name") == p.get("outcome")]
            if not mine or len(group) < 2:
                return None, None
            inv = [1 / d for d in (_american_to_decimal(o.get("price")) for o in group) if d]
            dec_mine = _american_to_decimal(mine[0].get("price"))
            if not dec_mine or len(inv) != len(group):
                return None, None
            return int(mine[0].get("price")), (1 / dec_mine) / sum(inv)
    return None, None


def maybe_capture_clv(fetch_event_odds: Callable[[str, str, str], dict],
                      remaining: Callable[[], Optional[int]]) -> None:
    if not CLV_ENABLED:
        return
    now = _now()
    with _db() as db:
        rows = db.all(
            "SELECT * FROM picks WHERE closing_at IS NULL AND event_id IS NOT NULL "
            "AND start_time > ? AND start_time <= ?",
            (_iso(now), _iso(now + timedelta(minutes=CLV_WINDOW_MIN))),
        )
        groups: dict[tuple, list] = {}
        for p in rows:
            groups.setdefault((p["sport_key"], p["event_id"], p["market"]), []).append(p)
        for (sk, ev_id, market), picks in groups.items():
            rem = remaining()
            if rem is None or rem < HISTORY_MIN_REMAINING:
                logger.info("Historial: CLV salteado (créditos restantes: %s)", rem)
                return
            try:
                data = fetch_event_odds(sk, ev_id, MARKET_API_KEY[market])
            except Exception as e:
                logger.warning("Historial: cierre de %s/%s falló: %s", sk, ev_id, e)
                data = {}
            for p in picks:
                c_odds, c_prob = closing_fair(p, data or {})
                dec = p.get("decimal_odds") or _american_to_decimal(p.get("odds"))
                clv = round((dec * c_prob - 1) * 100, 2) if (dec and c_prob) else None
                db.exec("UPDATE picks SET closing_odds = ?, closing_fair_prob = ?, clv = ?, closing_at = ? WHERE id = ?",
                        (c_odds, round(c_prob * 100, 2) if c_prob else None, clv, _iso(now), p["id"]))

# ---------------------------------------------------------------------------
# Punto de entrada desde get_dashboard (en segundo plano)
# ---------------------------------------------------------------------------
_last_check = 0.0
_run_lock = threading.Lock()


def _run(dash: dict, include_top: bool, sport_keys: dict, fetch_scores, fetch_event_odds, remaining) -> None:
    global _last_check
    if not _run_lock.acquire(blocking=False):
        return  # ya hay una pasada en curso
    try:
        try:
            n = record(dash, include_top, sport_keys)
            if n:
                logger.info("Historial: %d picks guardados/actualizados", n)
        except Exception as e:
            logger.warning("Historial: no se pudieron guardar picks: %s", e)
            return
        if time.time() - _last_check < CHECK_EVERY_S:
            return
        _last_check = time.time()
        try:
            maybe_grade(fetch_scores, remaining)
        except Exception as e:
            logger.warning("Historial: error calificando: %s", e)
        try:
            maybe_capture_clv(fetch_event_odds, remaining)
        except Exception as e:
            logger.warning("Historial: error en CLV: %s", e)
    finally:
        _run_lock.release()


def process_dashboard(dash: dict, include_top: bool, sport_keys: dict,
                      fetch_scores: Callable[[str], list],
                      fetch_event_odds: Callable[[str, str, str], dict],
                      remaining: Callable[[], Optional[int]]) -> None:
    """Guarda/califica en un hilo aparte para no demorar la respuesta."""
    if not HISTORY_ENABLED:
        return
    snap = {k: list(dash.get(k) or []) for k in ("top_plays", "efficiency_green", "efficiency_blue", "high_odds")}
    threading.Thread(
        target=_run, args=(snap, include_top, dict(sport_keys), fetch_scores, fetch_event_odds, remaining),
        daemon=True, name="history",
    ).start()

# ---------------------------------------------------------------------------
# Consulta para la pestaña Historial
# ---------------------------------------------------------------------------
TIER_FILTERS = {
    "top":   "is_top = 1",
    "green": "is_eff = 1 AND color = 'green'",
    "blue":  "is_eff = 1 AND color = 'blue'",
    "high":  "is_high = 1",
}
TIER_LABELS = {"top": "⭐ Mejores Jugadas", "green": "🔒 Sólido", "blue": "📌 Probable", "high": "💰 Cuotas Altas"}


def _summary(rows: list[dict]) -> dict:
    won = sum(1 for r in rows if r["status"] == "won")
    lost = sum(1 for r in rows if r["status"] == "lost")
    push = sum(1 for r in rows if r["status"] == "push")
    graded = won + lost + push
    units = round(sum((r.get("profit") or 0) for r in rows if r["status"] in ("won", "lost", "push")), 2)
    clvs = [r["clv"] for r in rows if r.get("clv") is not None]
    return {
        "total": len(rows),
        "pending": sum(1 for r in rows if r["status"] == "pending"),
        "no_result": sum(1 for r in rows if r["status"] == "no_result"),
        "won": won, "lost": lost, "push": push, "graded": graded,
        "hit_rate": round(won / (won + lost) * 100, 1) if (won + lost) else None,
        "units": units,
        "profit_per_10": round(units * 10, 2),
        "roi": round(units / graded * 100, 1) if graded else None,
        "avg_clv": round(sum(clvs) / len(clvs), 2) if clvs else None,
        "clv_count": len(clvs),
    }


def query(sport: Optional[str] = None, tier: Optional[str] = None,
          date_from: Optional[str] = None, date_to: Optional[str] = None,
          limit: int = 300) -> dict:
    where, params = [], []
    if sport and sport != "ALL":
        where.append("sport = ?")
        params.append(sport)
    if tier in TIER_FILTERS:
        where.append(TIER_FILTERS[tier])
    if date_from:
        where.append("pick_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("pick_date <= ?")
        params.append(date_to)
    sql = "SELECT * FROM picks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY start_time DESC"
    with _db() as db:
        rows = db.all(sql, tuple(params))
        next_calls = {r["k"].split(":", 1)[1]: r["v"]
                      for r in db.all("SELECT k, v FROM meta WHERE k LIKE ?", ("scores_last:%",))}

    by_tier = {k: _summary([r for r in rows if _in_tier(r, k)]) for k in TIER_FILTERS}
    by_sport: dict[str, list] = {}
    for r in rows:
        by_sport.setdefault(r["sport"], []).append(r)
    return {
        "summary": _summary(rows),
        "by_tier": [{"tier": k, "label": TIER_LABELS[k], **v} for k, v in by_tier.items() if v["total"]],
        "by_sport": sorted(({"sport": k, **_summary(v)} for k, v in by_sport.items()),
                           key=lambda x: x["total"], reverse=True),
        "rows": rows[:max(1, min(limit, 1000))],
        "truncated": len(rows) > limit,
        "scores_last": next_calls,
    }


def _in_tier(r: dict, tier: str) -> bool:
    if tier == "top":
        return r.get("is_top") == 1
    if tier == "green":
        return r.get("is_eff") == 1 and r.get("color") == "green"
    if tier == "blue":
        return r.get("is_eff") == 1 and r.get("color") == "blue"
    if tier == "high":
        return r.get("is_high") == 1
    return False


def config() -> dict:
    return {
        "enabled": HISTORY_ENABLED,
        "backend": backend_name(),
        "persistent": _PG,
        "scores_interval_h": SCORES_INTERVAL_H,
        "scores_cost": SCORES_COST,
        "min_remaining": HISTORY_MIN_REMAINING,
        "grade_delay_h": GRADE_DELAY_H,
        "clv_enabled": CLV_ENABLED,
        "clv_window_min": CLV_WINDOW_MIN,
    }
