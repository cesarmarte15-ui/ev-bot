"""
Cliente delgado de la MLB Stats API (statsapi.mlb.com, publica, sin key).
Devuelve estructuras Python planas (dicts/listas) — la mayoria de las
funciones no cachean nada por si mismas, eso lo orquestan los callers
(build_training_data.py, daily_score.py, backfill_outcomes.py) via
store.cached_call segun que tan cara/reutilizable es cada llamada. La
excepcion es fetch_boxscore_pitchers, que se auto-cachea por game_pk
porque un mismo partido historico se re-consulta muchas veces al armar
ventanas trailing de bullpen (ver su docstring).

Verificado contra la API real (2025-04-01/02, game_pk 778498):
- /v1/schedule ya trae score/isWinner/probablePitcher/dayNight/venue por
  partido para juegos Final — no hace falta boxscore para eso.
- /v1.1/game/{pk}/feed/live -> liveData.boxscore.teams.{home,away}.pitchers
  es una lista de ids en el orden en que aparecieron; el primero es el
  abridor real (verificado: coincide con probablePitcher en el caso feliz,
  pero es la fuente correcta para detectar scratches). El resto de la
  lista son los pitchers de bullpen usados, en orden de entrada.
- /v1/people/{id} -> people[0].pitchHand.code ("L"/"R").
"""
import logging
from functools import lru_cache
from typing import Optional

import requests

from . import config
from .store import cached_call, retry_with_backoff

logger = logging.getLogger("model.mlb_schedule")

_SESSION = requests.Session()


def _get(path: str, params: Optional[dict] = None, timeout: int = 20) -> dict:
    def _do():
        r = _SESSION.get(f"{config.MLB_STATS_API_BASE}{path}", params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    return retry_with_backoff(_do, attempts=config.RETRY_ATTEMPTS, base_delay=config.RETRY_BASE_DELAY_SECONDS)


def _flatten_game(game: dict) -> dict:
    away = game["teams"]["away"]
    home = game["teams"]["home"]
    away_pp = away.get("probablePitcher") or {}
    home_pp = home.get("probablePitcher") or {}
    venue = game.get("venue") or {}
    return {
        "game_pk":        game["gamePk"],
        "game_date":      game["gameDate"],       # ISO datetime UTC
        "official_date":  game["officialDate"],   # YYYY-MM-DD, fecha "de calendario"
        "season":         int(game["season"]),
        "game_type":      game["gameType"],        # "R" = regular season
        "status":         game["status"]["abstractGameState"],  # Preview/Live/Final
        "day_night":      game.get("dayNight"),
        "venue_id":       venue.get("id"),
        "venue_name":     venue.get("name"),
        "away_team_id":   away["team"]["id"],
        "away_team_name": away["team"]["name"],
        "home_team_id":   home["team"]["id"],
        "home_team_name": home["team"]["name"],
        "away_score":     away.get("score"),
        "home_score":     home.get("score"),
        "away_is_winner": away.get("isWinner"),
        "home_is_winner": home.get("isWinner"),
        "away_probable_pitcher_id":   away_pp.get("id"),
        "away_probable_pitcher_name": away_pp.get("fullName"),
        "home_probable_pitcher_id":   home_pp.get("id"),
        "home_probable_pitcher_name": home_pp.get("fullName"),
    }


def fetch_schedule_range(start_date: str, end_date: str, sport_id: int = 1) -> list[dict]:
    """
    start_date/end_date en formato YYYY-MM-DD (inclusive). Devuelve un dict
    plano por partido (ver _flatten_game). No filtra por gameType/status —
    eso lo decide el caller (training quiere gameType=="R" y status=="Final",
    daily scoring quiere la fecha de hoy tal cual venga).
    """
    data = _get("/v1/schedule", params={
        "sportId": sport_id,
        "startDate": start_date,
        "endDate": end_date,
        "hydrate": "probablePitcher,team,linescore,venue",
    })
    games = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            games.append(_flatten_game(game))
    return games


def fetch_boxscore_starters(game_pk: int) -> dict:
    """
    Abridor REAL (no probable) de cada lado, segun el orden de pitchers en
    el boxscore del live feed. Para partidos historicos esto es un hecho
    ya ocurrido (no es lookahead) — se usa en training para no depender del
    "probable pitcher" pregame, que puede diferir por un scratch.
    """
    data = _get(f"/v1.1/game/{game_pk}/feed/live")
    box = data["liveData"]["boxscore"]

    def _starter(side: str) -> tuple[Optional[int], Optional[str]]:
        pitcher_ids = box["teams"][side].get("pitchers") or []
        if not pitcher_ids:
            return None, None
        pid = pitcher_ids[0]
        player = box["teams"][side]["players"].get(f"ID{pid}", {})
        return pid, player.get("person", {}).get("fullName")

    away_id, away_name = _starter("away")
    home_id, home_name = _starter("home")
    return {
        "game_pk": game_pk,
        "away_starter_id": away_id,
        "away_starter_name": away_name,
        "home_starter_id": home_id,
        "home_starter_name": home_name,
    }


def fetch_boxscore_pitchers(game_pk: int) -> dict:
    """
    Lista COMPLETA (no solo el primero) de pitcher_ids usados por cada lado,
    en el orden real de aparicion — el primero de cada lista es el abridor
    (ver fetch_boxscore_starters), el resto salio del bullpen. La usa
    features._team_bullpen_pitcher_ids para armar el bullpen trailing de
    un equipo.

    Cacheada a disco por game_pk (data/raw/boxscore/{game_pk}.json), a
    diferencia de fetch_boxscore_starters: un mismo partido historico se
    vuelve a consultar una vez por cada partido POSTERIOR del mismo equipo
    cuya ventana trailing lo incluya (~STARTER_TRAILING_DAYS/dias-entre-
    partidos veces), asi que sin cache multiplicaria las llamadas de red.
    """
    cache_path = config.RAW_DIR / "boxscore" / f"{game_pk}.json"

    def _fetch():
        data = _get(f"/v1.1/game/{game_pk}/feed/live")
        box = data["liveData"]["boxscore"]
        return {
            "home": box["teams"]["home"].get("pitchers") or [],
            "away": box["teams"]["away"].get("pitchers") or [],
        }

    return cached_call(cache_path, _fetch, fmt="json")


def fetch_final_score(game_pk: int) -> Optional[dict]:
    """
    Para backfill_outcomes.py. Devuelve None si el partido todavia no esta
    Final (se reintenta en la proxima corrida programada).
    """
    data = _get(f"/v1.1/game/{game_pk}/feed/live")
    status = data["gameData"]["status"]["abstractGameState"]
    if status != "Final":
        return None
    linescore = data["liveData"]["linescore"]
    home_runs = linescore["teams"]["home"].get("runs")
    away_runs = linescore["teams"]["away"].get("runs")
    if home_runs is None or away_runs is None:
        return None
    return {
        "game_pk": game_pk,
        "status": status,
        "home_score": home_runs,
        "away_score": away_runs,
        "home_win": home_runs > away_runs,
    }


@lru_cache(maxsize=4096)
def fetch_person_hand(player_id: int) -> Optional[str]:
    """"L"/"R", o None si no esta disponible. Cacheado en memoria por
    proceso (lru_cache) — un mismo pitcher aparece en decenas de partidos."""
    data = _get(f"/v1/people/{player_id}")
    people = data.get("people") or []
    if not people:
        return None
    return people[0].get("pitchHand", {}).get("code")
