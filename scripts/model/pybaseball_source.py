"""
Wrapper cacheado sobre pybaseball. Devuelve DataFrames crudos (mas o menos
tal cual los da pybaseball, con limpieza minima) — la logica de "que
snapshot/ventana usar para el partido X" vive en features.py, no aca.

Verificado a mano contra pybaseball 2.2.7 (2026-08-16):
- pitching_stats_range(start_dt, end_dt) trae columna 'mlbID' 100% poblada
  -> se puede joinear directo contra el pitcher id de MLB Stats API, sin
  necesidad de un crosswalk de nombres via chadwick_register(). No incluye
  FIP: se calcula a mano en features.py a partir de HR/BB/HBP/SO/IP.
- batting_stats_range() usa 'Tm' como nombre de CIUDAD (ej. "Chicago",
  "Los Angeles"), AMBIGUO entre equipos de la misma ciudad, y ademas junta
  con comas ("Atlanta,Houston") para jugadores traspasados en la ventana.
  Es INUTIL para agregar a nivel de equipo sin un mapeo adicional fragil ->
  no se usa. En su lugar, tanto team_form_diff como offense_diff se derivan
  de schedule_and_record (runs allowed / runs scored por partido, ya
  identificado sin ambiguedad por abreviatura de Baseball-Reference).
- schedule_and_record(season, team_abbr) requiere pandas<3.0 (pandas 3.0
  rompe con un ValueError al parsear la columna Attendance — ver
  requirements-model.txt). La columna 'Date' no trae anio ("Thursday, Mar
  27"); se reconstruye con el 'season' pasado como parametro.
"""
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import pybaseball as pb

from . import config
from .store import cached_call, retry_with_backoff

logger = logging.getLogger("model.pybaseball_source")

pb.cache.enable()


_EMPTY_PITCHING_COLUMNS = ["Name", "Tm", "GS", "IP", "ERA", "HR", "BB", "HBP", "SO", "mlbID"]


def get_pitching_snapshot(start_dt: str, end_dt: str) -> pd.DataFrame:
    """
    Snapshot FanGraphs de pitcheo para el rango [start_dt, end_dt]
    ("YYYY-MM-DD"). Cacheado a data/raw/fangraphs_pitching/{start}_{end}.parquet.
    Columnas relevantes: Name, Tm, GS, IP, ERA, HR, BB, HBP, SO, mlbID.

    pybaseball.pitching_stats_range revienta con IndexError (no una
    excepcion de red) cuando FanGraphs no tiene tabla para el rango pedido
    — pasa tipicamente en partidos de la primera semana de temporada, cuya
    ventana trailing de 30 dias cae antes de que exista data de temporada
    regular. No es transitorio: reintentar no cambia nada, asi que se
    detecta aparte (sin pasar por retry_with_backoff) y se cachea como
    snapshot vacio, para que ademas no se reintente el scrape cada vez que
    otro partido de esa misma semana llegue a pedir la misma ventana.
    """
    cache_path = config.RAW_DIR / "fangraphs_pitching" / f"{start_dt}_{end_dt}.parquet"

    def _pull():
        return pb.pitching_stats_range(start_dt, end_dt)

    def _fetch():
        try:
            df = _pull()
        except IndexError:
            logger.info("Sin tabla de FanGraphs para %s..%s (rango sin temporada regular?), snapshot vacio",
                        start_dt, end_dt)
            df = pd.DataFrame(columns=_EMPTY_PITCHING_COLUMNS)
        except Exception:
            df = retry_with_backoff(_pull, attempts=config.RETRY_ATTEMPTS, base_delay=config.RETRY_BASE_DELAY_SECONDS)

        df = df.copy()
        # mlbID viene como string (dtype object) de pybaseball; se castea a
        # Int64 nullable para poder joinear contra los ids int de MLB Stats
        # API sin fallar el match por tipo.
        if df.empty:
            df["mlbID"] = pd.array([], dtype="Int64")
        else:
            df["mlbID"] = pd.to_numeric(df["mlbID"], errors="coerce").astype("Int64")
        return df

    return cached_call(cache_path, _fetch, fmt="parquet")


def _parse_schedule_date(date_str: str, season: int) -> "datetime | None":
    # Formato tipico: "Thursday, Mar 27". Puede traer sufijo de doubleheader
    # ("Mar 27 (1)") en algunas temporadas/equipos — se limpia si aparece.
    cleaned = date_str.replace(" (1)", "").replace(" (2)", "").strip()
    try:
        return datetime.strptime(f"{cleaned} {season}", "%A, %b %d %Y")
    except ValueError:
        logger.warning("No se pudo parsear fecha de schedule_and_record: %r (season=%s)", date_str, season)
        return None


def get_team_game_log(season: int, team_abbr: str) -> pd.DataFrame:
    """
    Log partido-a-partido (Baseball-Reference) de un equipo en una
    temporada: fecha real (columna 'game_date', derivada), runs scored
    ('R') y runs allowed ('RA'). Cacheado a
    data/raw/schedule_and_record/{season}/{team_abbr}.parquet.
    """
    cache_path = config.RAW_DIR / "schedule_and_record" / str(season) / f"{team_abbr}.parquet"

    def _fetch():
        df = retry_with_backoff(
            lambda: pb.schedule_and_record(season, team_abbr),
            attempts=config.RETRY_ATTEMPTS, base_delay=config.RETRY_BASE_DELAY_SECONDS,
        )
        df = df.copy()
        df["game_date"] = df["Date"].apply(lambda s: _parse_schedule_date(s, season))
        df = df.dropna(subset=["game_date", "R", "RA"])
        df = df.sort_values("game_date").reset_index(drop=True)
        return df[["game_date", "Opp", "Home_Away", "R", "RA", "W/L"]]

    return cached_call(cache_path, _fetch, fmt="parquet")
