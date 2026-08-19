"""
CLI: python -m scripts.model.daily_score [--date YYYY-MM-DD] [--force]

Scorea los partidos de hoy (o de --date) con el modelo entrenado
(artifacts/model.joblib) y loguea cada prediccion en logs/predictions.csv,
junto con el contexto de mercado que ya calcula la app de EV Bot en
produccion (EV_BOT_GAMES_URL), para poder comparar despues modelo vs
mercado vs resultado real. predictions.csv es un log INMUTABLE una vez
escrita una fila: el resultado real se completa en outcomes.csv aparte
(backfill_outcomes.py, no escrito todavia), nunca pisando esta fila.

Diferencias con build_training_data.py (mismo features.build_feature_row,
nunca duplicado, ver features.py):
- before_date=hoy, siempre "trivialmente seguro" (nada de hoy paso
  todavia, ver docstring de modulo en features.py).
- El abridor usado es el PROBABLE (probablePitcher del schedule), no el
  real via boxscore: el partido todavia no se jugo. Un scratch de ultima
  hora despues de correr este script no se refleja — limitacion aceptada,
  no vale la pena repollear en vivo por eso.
- El schedule de la temporada (necesario para rest_days_diff y para
  encontrar bullpens trailing) NO se cachea indefinidamente a diferencia
  de build_training_data.py: la temporada en curso sigue sumando partidos
  Final dia a dia, cachearlo para siempre lo dejaria stale. Se cachea a
  nivel de UN dia de corrida (cache_path incluye la fecha), asi reintentos
  manuales el mismo dia no vuelven a pegarle a la red pero el dia
  siguiente si.

Resumability: predictions.csv actua como checkpoint por (date, game_pk) —
si el game_pk ya esta logueado para esa fecha, se saltea por default (evita
filas duplicadas si el cron dispara dos veces). --force fuerza una fila
nueva igual (util para tomar una segunda foto del mismo dia y comparar
movimiento de linea intra-dia), nunca pisa ni borra las anteriores.
"""
import argparse
import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import joblib
import pandas as pd
import requests

from . import config, features, mlb_schedule, store

logger = logging.getLogger("model.daily_score")

_PREDICTIONS_FIELDS = [
    "date", "game_pk", "home_team", "away_team",
    "home_starter_id", "away_starter_id",
    "model_home_win_prob",
    "market_home_prob", "market_home_odds",
    "market_away_prob", "market_away_odds",
    "market_matched",
    "scored_at",
]


def _today(tz: str = config.TZ) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def _season_schedule_to_date(season: int, through: date) -> list[dict]:
    """Schedule de la temporada desde el 1 de febrero hasta 'through'
    (inclusive), cacheado por dia de corrida — ver nota de staleness en el
    docstring del modulo."""
    cache_path = config.RAW_DIR / "schedule" / "daily" / f"{through.isoformat()}.parquet"

    def _fetch():
        games = mlb_schedule.fetch_schedule_range(f"{season}-02-01", through.isoformat())
        return pd.DataFrame(games)

    df = store.cached_call(cache_path, _fetch, fmt="parquet")
    return df.to_dict("records")


def _load_seen_game_pks(csv_path: Path, today: date) -> set:
    if not csv_path.exists():
        return set()
    seen = set()
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["date"] == today.isoformat():
                seen.add(int(row["game_pk"]))
    return seen


def _fetch_market_games() -> dict:
    """(home_team, away_team) -> game dict de /api/games, solo MLB. Si el
    fetch falla (ej. app dormida en el free tier de Render), se loguea un
    warning y se sigue sin contexto de mercado: el score del modelo no
    depende de esto, es contexto adicional para el log."""
    try:
        r = requests.get(config.EV_BOT_GAMES_URL, params={"sports": "MLB"}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("No se pudo obtener mercado de %s (%s), predictions sin contexto de mercado",
                        config.EV_BOT_GAMES_URL, e)
        return {}

    out = {}
    for g in data.get("all_games", []):
        if g.get("sport") != "MLB":
            continue
        out[(g.get("home_team"), g.get("away_team"))] = g
    return out


def _market_side(ml_signals: Optional[list], team_name: str) -> tuple[Optional[float], Optional[int]]:
    for sig in ml_signals or []:
        if sig.get("selection") == team_name:
            return sig.get("probability"), sig.get("odds")
    return None, None


def score_day(today: date, force: bool) -> None:
    config.ensure_dirs()
    season = today.year

    games_today = mlb_schedule.fetch_schedule_range(today.isoformat(), today.isoformat())
    games_today = [g for g in games_today if g["game_type"] == "R"]
    live_or_final = [g for g in games_today if g["status"] != "Preview"]
    games_today = [g for g in games_today if g["status"] == "Preview"]
    if live_or_final:
        logger.info("%s: %d partidos de temporada regular ya en vivo/finales, se saltean (solo se scorea pregame)",
                     today, len(live_or_final))
    if not games_today:
        logger.info("%s: no hay partidos de temporada regular pregame", today)
        return

    season_games = _season_schedule_to_date(season, today)

    seen = _load_seen_game_pks(config.PREDICTIONS_LOG_PATH, today)
    if seen and not force:
        logger.info("%s: %d partidos ya logueados hoy, se saltean (--force para tomar otra foto)", today, len(seen))

    pipeline = joblib.load(config.MODEL_PATH)
    market = _fetch_market_games()

    rows_to_score = []
    for g in games_today:
        game_pk = g["game_pk"]
        if game_pk in seen and not force:
            continue

        home_starter = g.get("home_probable_pitcher_id")
        away_starter = g.get("away_probable_pitcher_id")
        if home_starter is None or away_starter is None:
            logger.info("game_pk %s: sin abridor probable confirmado todavia, se saltea", game_pk)
            continue

        ctx = features.GameContext(
            game_pk=game_pk,
            date=today,
            season=season,
            home_team_id=g["home_team_id"],
            away_team_id=g["away_team_id"],
            home_starter_id=home_starter,
            away_starter_id=away_starter,
            venue_id=g["venue_id"],
            day_night=g["day_night"],
            home_win=None,
        )
        row = features.build_feature_row(ctx, season_games)
        if row is None:
            continue
        rows_to_score.append((g, row))

    if not rows_to_score:
        logger.info("%s: 0 partidos scoreables (sin abridor probable confirmado o sin cobertura de features)", today)
        return

    X = pd.DataFrame([row for _, row in rows_to_score])[features.FEATURE_NAMES]
    proba = pipeline.predict_proba(X)[:, 1]

    is_new_file = not config.PREDICTIONS_LOG_PATH.exists()
    with open(config.PREDICTIONS_LOG_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_PREDICTIONS_FIELDS)
        if is_new_file:
            writer.writeheader()

        for (g, _row), home_win_prob in zip(rows_to_score, proba):
            key = (g["home_team_name"], g["away_team_name"])
            mkt_game = market.get(key)
            market_home_prob = market_home_odds = market_away_prob = market_away_odds = None
            if mkt_game:
                market_home_prob, market_home_odds = _market_side(mkt_game.get("ml"), g["home_team_name"])
                market_away_prob, market_away_odds = _market_side(mkt_game.get("ml"), g["away_team_name"])

            writer.writerow({
                "date": today.isoformat(),
                "game_pk": g["game_pk"],
                "home_team": g["home_team_name"],
                "away_team": g["away_team_name"],
                "home_starter_id": g.get("home_probable_pitcher_id"),
                "away_starter_id": g.get("away_probable_pitcher_id"),
                "model_home_win_prob": round(float(home_win_prob), 4),
                "market_home_prob": market_home_prob,
                "market_home_odds": market_home_odds,
                "market_away_prob": market_away_prob,
                "market_away_odds": market_away_odds,
                "market_matched": mkt_game is not None,
                "scored_at": datetime.now(ZoneInfo(config.TZ)).isoformat(),
            })

    logger.info("%s: %d partidos scoreados -> %s", today, len(rows_to_score), config.PREDICTIONS_LOG_PATH)


def main():
    parser = argparse.ArgumentParser(description="Scorea los partidos de hoy con el modelo entrenado")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD; default hoy en America/New_York")
    parser.add_argument("--force", action="store_true",
                         help="Loguea otra fila para partidos ya scoreados hoy (no pisa las previas).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    today = date.fromisoformat(args.date) if args.date else _today()
    score_day(today, force=args.force)


if __name__ == "__main__":
    main()
