"""
UNICA fuente de feature engineering del pipeline — la usan tanto
build_training_data.py como daily_score.py, para que entrenamiento y
scoring nunca diverjan en logica.

Regla anti-lookahead (estructural, no solo convencion): toda funcion que
mira datos historicos recibe un parametro explicito 'before_date' y solo
usa filas con fecha estrictamente anterior a esa fecha. Para training
'before_date' es la fecha real del partido; para scoring diario es "hoy"
(trivialmente seguro, porque nada del dia de hoy paso todavia).

Politica de datos faltantes (asimetrica, deliberada):
- starter_fip_diff, team_form_diff/offense_diff y bullpen_fip_diff (las
  features con contenido predictivo real, que requieren historial no
  trivial) hacen DROP del partido entero (build_feature_row -> None) si
  no hay cobertura suficiente. Es la fuente esperada de perdida de
  partidos de abril temprano / call-ups mencionada en el plan.
- park_factor, *_starter_lefty y day_night son features menores/estaticas;
  ante un dato faltante puntual usan un default neutral documentado (no
  tiran el partido entero) — el costo de perder una fila completa por un
  dato demografico menor no vale la pena frente al ruido que introduce.
"""
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from . import config, mlb_schedule, pybaseball_source, team_reference

logger = logging.getLogger("model.features")

FEATURE_NAMES: list[str] = [
    "starter_fip_diff",
    "bullpen_fip_diff",
    "team_form_diff",
    "offense_diff",
    "rest_days_diff",
    "park_factor",
    "home_starter_lefty",
    "away_starter_lefty",
    "day_night",
]

_DEFAULT_PARK_FACTOR = 1.00
_DEFAULT_REST_DAYS = 1  # asumido cuando no hay partido previo en los datos (ej. Opening Day)
_REST_DAYS_CLAMP = (-3, 3)


@dataclass
class GameContext:
    game_pk: int
    date: date
    season: int
    home_team_id: int
    away_team_id: int
    home_starter_id: Optional[int]
    away_starter_id: Optional[int]
    venue_id: Optional[int]
    day_night: Optional[str]
    home_win: Optional[int] = None  # label; None si el partido todavia no se jugo


def _week_anchor(d: date) -> date:
    """Lunes de la semana de d. Todos los partidos de una misma semana
    comparten el mismo snapshot de FanGraphs (ver docstring de modulo en
    build_training_data.py) — hasta ~1 semana de staleness aceptada a
    cambio de ~90x menos llamadas de scraping."""
    return d - timedelta(days=d.weekday())


def _snapshot_window(before_date: date) -> tuple[str, str]:
    anchor = _week_anchor(before_date)
    end = anchor - timedelta(days=1)
    start = end - timedelta(days=config.STARTER_TRAILING_DAYS - 1)
    return start.isoformat(), end.isoformat()


def _starter_fip_core(pitcher_id: Optional[int], before_date: date) -> Optional[float]:
    """FIP sin la constante de liga (13*HR + 3*(BB+HBP) - 2*SO) / IP.
    Se omite la constante deliberadamente: starter_fip_diff SIEMPRE resta
    dos pitchers del MISMO snapshot semanal (mismo before_date -> mismo
    anchor), asi que la constante aditiva se cancela exactamente en la
    resta. Evita tener que recalcular una constante de liga por snapshot."""
    if pitcher_id is None:
        return None
    start, end = _snapshot_window(before_date)
    snap = pybaseball_source.get_pitching_snapshot(start, end)
    rows = snap[snap["mlbID"] == pitcher_id]
    if rows.empty:
        return None
    row = rows.iloc[0]
    gs = row.get("GS", 0)
    ip = row.get("IP", 0.0)
    if gs is None or gs < config.MIN_TRAILING_STARTS or ip is None or ip <= 0:
        return None
    hr, bb, hbp, so = row.get("HR", 0), row.get("BB", 0), row.get("HBP", 0), row.get("SO", 0)
    if any(v is None for v in (hr, bb, hbp, so)):
        return None
    return (13 * hr + 3 * (bb + hbp) - 2 * so) / ip


def _team_bullpen_pitcher_ids(team_id: int, before_date: date, season_games: list[dict]) -> set[int]:
    """Pitcher_ids que salieron del bullpen (no arrancaron) por team_id en
    sus partidos de temporada regular ya finalizados, estrictamente antes
    de before_date, dentro de la misma ventana de STARTER_TRAILING_DAYS
    que usa el FIP trailing del abridor (mismo antes/despues, misma
    nocion de 'reciente'). Usa el boxscore real de cada partido (cacheado
    por game_pk via mlb_schedule.fetch_boxscore_pitchers) — un pitcher que
    aparece como abridor en OTRO partido de la ventana igual puede sumar
    aca si en este partido puntual salio del bullpen (swingman); se acepta
    esa aproximacion, ver _bullpen_fip_core."""
    start, end = _snapshot_window(before_date)
    window_start, window_end = date.fromisoformat(start), date.fromisoformat(end)
    team_games = [
        g for g in season_games
        if g["game_type"] == "R" and g["status"] == "Final"
        and (g["home_team_id"] == team_id or g["away_team_id"] == team_id)
        and window_start <= date.fromisoformat(g["official_date"]) <= window_end
    ]
    reliever_ids: set[int] = set()
    for g in team_games:
        side = "home" if g["home_team_id"] == team_id else "away"
        try:
            box = mlb_schedule.fetch_boxscore_pitchers(g["game_pk"])
        except Exception as e:
            logger.warning("game_pk %s: fallo boxscore de bullpen (%s), se omite ese partido de la ventana",
                            g["game_pk"], e)
            continue
        reliever_ids.update(box[side][1:])  # todos menos el abridor (indice 0)
    return reliever_ids


def _bullpen_fip_core(team_id: int, before_date: date, season_games: list[dict]) -> Optional[float]:
    """FIP sin constante de liga, agregado sobre TODOS los relevistas
    usados por team_id en la ventana trailing (misma cancelacion de
    constante que _starter_fip_core: siempre resta dos bullpens del mismo
    snapshot semanal). None si no hay relevistas identificados o si las IP
    totales de bullpen en la ventana no llegan a MIN_BULLPEN_TRAILING_IP
    (muestra insuficiente para una tasa estable)."""
    reliever_ids = _team_bullpen_pitcher_ids(team_id, before_date, season_games)
    if not reliever_ids:
        return None
    start, end = _snapshot_window(before_date)
    snap = pybaseball_source.get_pitching_snapshot(start, end)
    rows = snap[snap["mlbID"].isin(reliever_ids)]
    if rows.empty:
        return None
    ip = rows["IP"].sum()
    if ip is None or ip < config.MIN_BULLPEN_TRAILING_IP:
        return None
    hr, bb, hbp, so = rows["HR"].sum(), rows["BB"].sum(), rows["HBP"].sum(), rows["SO"].sum()
    return (13 * hr + 3 * (bb + hbp) - 2 * so) / ip


def _team_trailing_form(team_id: int, season: int, before_date: date) -> Optional[dict]:
    """Promedio de runs allowed / runs scored de los ultimos
    TEAM_FORM_TRAILING_GAMES partidos del equipo estrictamente antes de
    before_date. None si no hay NINGUN partido previo en la temporada
    (equipo sin historial) — no confundir con el default de
    _DEFAULT_REST_DAYS, que es para rest_days, no para esta feature."""
    abbr = team_reference.bbref_abbr(team_id, season)
    log = pybaseball_source.get_team_game_log(season, abbr)
    before_ts = pd.Timestamp(before_date)
    prior = log[log["game_date"] < before_ts]
    if prior.empty:
        return None
    trailing = prior.tail(config.TEAM_FORM_TRAILING_GAMES)
    return {
        "runs_allowed_avg": float(trailing["RA"].mean()),
        "runs_scored_avg": float(trailing["R"].mean()),
    }


def _rest_days(team_id: int, season: int, game_date: date, season_games: list[dict]) -> int:
    """Dias desde el partido anterior del equipo (home o away) antes de
    game_date, buscando en season_games (schedule completo de la
    temporada, ya pulleado por el caller). Si no hay partido previo
    (Opening Day), usa _DEFAULT_REST_DAYS en vez de descartar el partido:
    perder todos los Opening Day del dataset por una feature secundaria
    no vale la pena."""
    prior_dates = [
        date.fromisoformat(g["official_date"])
        for g in season_games
        if (g["home_team_id"] == team_id or g["away_team_id"] == team_id)
        and date.fromisoformat(g["official_date"]) < game_date
    ]
    if not prior_dates:
        return _DEFAULT_REST_DAYS
    return (game_date - max(prior_dates)).days


def _park_factor(venue_id: Optional[int]) -> float:
    if venue_id is None:
        return _DEFAULT_PARK_FACTOR
    factors = team_reference.load_park_factors()
    if venue_id not in factors:
        logger.warning("venue_id %s sin park factor conocido, uso default %.2f", venue_id, _DEFAULT_PARK_FACTOR)
        return _DEFAULT_PARK_FACTOR
    return factors[venue_id]


def _starter_lefty(pitcher_id: Optional[int]) -> int:
    if pitcher_id is None:
        return 0
    hand = mlb_schedule.fetch_person_hand(pitcher_id)
    if hand is None:
        logger.warning("Mano desconocida para pitcher_id %s, asumo diestro (default)", pitcher_id)
        return 0
    return 1 if hand == "L" else 0


def build_feature_row(ctx: GameContext, season_games: list[dict]) -> Optional[dict]:
    """
    season_games: schedule COMPLETO de la temporada de ctx (lista de dicts
    como los devuelve mlb_schedule.fetch_schedule_range), usado para
    calcular rest_days_diff y para encontrar los partidos previos del
    equipo al armar bullpen_fip_diff. Se pasa desde afuera en vez de
    refetchear porque el caller ya lo tiene cacheado a nivel de temporada.

    Devuelve None (logueando la razon) si starter_fip_diff, bullpen_fip_diff
    o team_form_diff/offense_diff no tienen cobertura suficiente — ver
    politica de datos faltantes en el docstring del modulo.
    """
    before_date = ctx.date

    away_fip = _starter_fip_core(ctx.away_starter_id, before_date)
    home_fip = _starter_fip_core(ctx.home_starter_id, before_date)
    if away_fip is None or home_fip is None:
        logger.info("game_pk %s: sin FIP trailing suficiente (away=%s home=%s), descartado",
                    ctx.game_pk, away_fip, home_fip)
        return None

    away_bullpen_fip = _bullpen_fip_core(ctx.away_team_id, before_date, season_games)
    home_bullpen_fip = _bullpen_fip_core(ctx.home_team_id, before_date, season_games)
    if away_bullpen_fip is None or home_bullpen_fip is None:
        logger.info("game_pk %s: sin bullpen trailing suficiente (away=%s home=%s), descartado",
                    ctx.game_pk, away_bullpen_fip, home_bullpen_fip)
        return None

    away_form = _team_trailing_form(ctx.away_team_id, ctx.season, before_date)
    home_form = _team_trailing_form(ctx.home_team_id, ctx.season, before_date)
    if away_form is None or home_form is None:
        logger.info("game_pk %s: sin forma reciente de equipo suficiente, descartado", ctx.game_pk)
        return None

    rest_home = _rest_days(ctx.home_team_id, ctx.season, ctx.date, season_games)
    rest_away = _rest_days(ctx.away_team_id, ctx.season, ctx.date, season_games)
    rest_days_diff = max(_REST_DAYS_CLAMP[0], min(_REST_DAYS_CLAMP[1], rest_home - rest_away))

    return {
        "game_pk": ctx.game_pk,
        "date": ctx.date.isoformat(),
        "starter_fip_diff": away_fip - home_fip,
        "bullpen_fip_diff": away_bullpen_fip - home_bullpen_fip,
        "team_form_diff": away_form["runs_allowed_avg"] - home_form["runs_allowed_avg"],
        "offense_diff": home_form["runs_scored_avg"] - away_form["runs_scored_avg"],
        "rest_days_diff": rest_days_diff,
        "park_factor": _park_factor(ctx.venue_id),
        "home_starter_lefty": _starter_lefty(ctx.home_starter_id),
        "away_starter_lefty": _starter_lefty(ctx.away_starter_id),
        "day_night": 1 if ctx.day_night == "night" else 0,
        "home_win": ctx.home_win,
    }
