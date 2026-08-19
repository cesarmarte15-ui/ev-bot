"""
CLI: python -m scripts.model.build_training_data --seasons 2023 2024 2025 [--resume]

Arma el dataset historico de entrenamiento, resumable:
1. Por temporada, pull (y cachea) el schedule completo via
   mlb_schedule.fetch_schedule_range — un solo archivo
   data/raw/schedule/{season}.parquet por temporada.
2. Filtra a partidos de temporada regular ya finalizados (game_type=="R",
   status=="Final").
3. Para cada partido no visto todavia, pull el abridor REAL
   (fetch_boxscore_starters, no el "probable") y arma una GameContext.
4. Llama features.build_feature_row(ctx, season_games) — LA MISMA funcion
   que usara daily_score.py, nunca duplicada.
5. Escribe cada fila (o nada, si build_feature_row la descarta) de forma
   incremental al jsonl de la temporada, asi una corrida interrumpida
   a mitad de camino no pierde el trabajo ya hecho.
6. Al final, compila los jsonl de las temporadas pedidas en el parquet
   combinado que usa train_model.py.

Resumability: training_rows_{season}.jsonl actua como checkpoint a nivel
de fila (se saltea cualquier game_pk ya presente). Si el archivo ya existe
y no se paso --resume, el script se niega a correr (para no continuar una
corrida vieja por accidente sin que el usuario lo pida explicitamente).

Fallas de red aisladas: mlb_schedule/pybaseball_source ya reintentan via
store.retry_with_backoff; si un game_pk se sigue cayendo, se anota en
_failed_game_pks.txt y se saltea sin abortar el resto del run.
"""
import argparse
import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from . import config, features, mlb_schedule, store

logger = logging.getLogger("model.build_training_data")

_SEASON_START_MONTH_DAY = "02-01"  # cubre spring training de sobra; se filtra por game_type despues
_SEASON_END_MONTH_DAY = "11-30"    # cubre postemporada de sobra; se filtra por game_type despues


def _season_schedule(season: int) -> list[dict]:
    """Schedule completo de la temporada (todos los game_type/status),
    cacheado a data/raw/schedule/{season}.parquet. Se usa tanto para
    iterar los partidos a featurizar como para el calculo de
    rest_days_diff (necesita ver TODOS los partidos del equipo, no solo
    los ya featurizados)."""
    cache_path = config.RAW_DIR / "schedule" / f"{season}.parquet"

    def _fetch():
        games = mlb_schedule.fetch_schedule_range(
            f"{season}-{_SEASON_START_MONTH_DAY}", f"{season}-{_SEASON_END_MONTH_DAY}",
        )
        return pd.DataFrame(games)

    df = store.cached_call(cache_path, _fetch, fmt="parquet")
    return df.to_dict("records")


def _load_seen_game_pks(jsonl_path: Path) -> set:
    if not jsonl_path.exists():
        return set()
    seen = set()
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(json.loads(line)["game_pk"])
            except (json.JSONDecodeError, KeyError):
                continue
    return seen


def build_season(season: int, resume: bool) -> None:
    config.ensure_dirs()
    jsonl_path = config.PROCESSED_DIR / f"training_rows_{season}.jsonl"

    if jsonl_path.exists() and not resume:
        raise RuntimeError(
            f"{jsonl_path} ya existe. Pasa --resume para continuar esta temporada, "
            f"o borra el archivo si queres empezar de cero."
        )

    season_games = _season_schedule(season)
    completed = [g for g in season_games if g["game_type"] == "R" and g["status"] == "Final"]
    logger.info("Temporada %d: %d partidos de temporada regular finalizados", season, len(completed))

    seen = _load_seen_game_pks(jsonl_path)
    if seen:
        logger.info("Temporada %d: %d partidos ya procesados en %s, se saltean", season, len(seen), jsonl_path.name)

    built = dropped = failed = 0
    with open(jsonl_path, "a", encoding="utf-8") as out:
        for i, g in enumerate(completed):
            game_pk = g["game_pk"]
            if game_pk in seen:
                continue

            home_winner = g.get("home_is_winner")
            if home_winner not in (True, False):
                logger.warning("game_pk %s: home_is_winner invalido (%r, posible empate/suspendido), se omite",
                                game_pk, home_winner)
                dropped += 1
                continue

            try:
                starters = mlb_schedule.fetch_boxscore_starters(game_pk)
            except Exception as e:
                logger.warning("game_pk %s: fallo boxscore tras reintentos (%s), anotado y saltado", game_pk, e)
                store.append_failed_key(config.FAILED_KEYS_PATH, f"boxscore:{game_pk}")
                failed += 1
                continue

            ctx = features.GameContext(
                game_pk=game_pk,
                date=date.fromisoformat(g["official_date"]),
                season=season,
                home_team_id=g["home_team_id"],
                away_team_id=g["away_team_id"],
                home_starter_id=starters["home_starter_id"],
                away_starter_id=starters["away_starter_id"],
                venue_id=g["venue_id"],
                day_night=g["day_night"],
                home_win=1 if home_winner else 0,
            )
            try:
                row = features.build_feature_row(ctx, season_games)
            except Exception as e:
                logger.warning("game_pk %s: fallo build_feature_row (%s), anotado y saltado", game_pk, e)
                store.append_failed_key(config.FAILED_KEYS_PATH, f"features:{game_pk}")
                failed += 1
                continue

            if row is None:
                dropped += 1
                continue

            out.write(json.dumps(row) + "\n")
            out.flush()
            built += 1

            if (i + 1) % 100 == 0:
                logger.info("Temporada %d: %d/%d procesados (built=%d dropped=%d failed=%d)",
                             season, i + 1, len(completed), built, dropped, failed)

    logger.info("Temporada %d completa: built=%d dropped=%d failed=%d", season, built, dropped, failed)


def dataset_path(seasons: list) -> Path:
    """Path del parquet combinado para un conjunto de temporadas, misma
    convencion de nombre usada por compile_dataset y train_model.py (para
    que ambos siempre miren el mismo archivo sin duplicar el join)."""
    return config.PROCESSED_DIR / f"training_data_{'_'.join(str(s) for s in seasons)}.parquet"


def compile_dataset(seasons: list) -> Path:
    frames = []
    for season in seasons:
        jsonl_path = config.PROCESSED_DIR / f"training_rows_{season}.jsonl"
        if not jsonl_path.exists():
            logger.warning("No existe %s, se omite de la compilacion", jsonl_path)
            continue
        rows = []
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["season"] = season
        frames.append(df)

    if not frames:
        raise RuntimeError("No hay datos procesados para compilar — corre build_season primero")

    combined = pd.concat(frames, ignore_index=True)
    out_path = dataset_path(seasons)
    combined.to_parquet(out_path)
    logger.info("Dataset combinado: %d filas -> %s", len(combined), out_path)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Arma el dataset historico de entrenamiento MLB")
    parser.add_argument("--seasons", type=int, nargs="+", default=config.SEASONS)
    parser.add_argument("--resume", action="store_true",
                         help="Continua una temporada cuyo jsonl ya existe (saltea game_pks ya procesados).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    for season in args.seasons:
        build_season(season, resume=args.resume)

    compile_dataset(args.seasons)


if __name__ == "__main__":
    main()
