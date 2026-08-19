"""
CLI: python -m scripts.model.backfill_outcomes

Completa logs/outcomes.csv con el resultado real de los partidos ya
logueados en logs/predictions.csv (ver daily_score.py) cuyo resultado
todavia no esta backfilleado. NUNCA toca predictions.csv: son logs
separados a proposito, unidos por game_pk en el analisis (join), asi el
log de predicciones queda inmutable una vez escrito (walk-forward puro,
sin resultado filtrandose hacia atras).

Checkpoint por game_pk (no por fecha, a diferencia de predictions.csv): un
game_pk real solo termina una vez, sin importar cuantas filas de
predictions.csv lo referencien (--force de daily_score.py puede loguear
mas de una fila del mismo partido en el mismo dia).

Partidos todavia no Final devuelven None (mlb_schedule.fetch_final_score)
y se saltean sin error: la proxima corrida programada los vuelve a
intentar. Fallas de red aisladas tras agotar los reintentos de
store.retry_with_backoff se anotan en _failed_game_pks.txt (mismo archivo
que build_training_data.py, prefijo "outcome:") y no abortan el resto del
run.
"""
import csv
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, mlb_schedule, store

logger = logging.getLogger("model.backfill_outcomes")

_OUTCOMES_FIELDS = [
    "date", "game_pk", "home_team", "away_team",
    "home_score", "away_score", "home_win",
    "backfilled_at",
]


def _load_pending_games(predictions_path: Path) -> dict:
    """game_pk -> {date, home_team, away_team} de la PRIMERA fila de
    predictions.csv que lo menciona (alcanza para el log de outcomes, que
    no depende de cual snapshot de prediccion se uso)."""
    if not predictions_path.exists():
        raise FileNotFoundError(f"No existe {predictions_path} — corre daily_score.py primero.")
    pending: dict = {}
    with open(predictions_path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            game_pk = int(row["game_pk"])
            if game_pk not in pending:
                pending[game_pk] = {
                    "date": row["date"],
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                }
    return pending


def _load_backfilled_game_pks(outcomes_path: Path) -> set:
    if not outcomes_path.exists():
        return set()
    with open(outcomes_path, "r", encoding="utf-8", newline="") as f:
        return {int(row["game_pk"]) for row in csv.DictReader(f)}


def backfill() -> None:
    config.ensure_dirs()
    pending = _load_pending_games(config.PREDICTIONS_LOG_PATH)
    done = _load_backfilled_game_pks(config.OUTCOMES_LOG_PATH)
    todo = {pk: meta for pk, meta in pending.items() if pk not in done}

    if not todo:
        logger.info("Nada para backfillear: %d partidos en predictions.csv, todos ya tienen outcome", len(pending))
        return

    logger.info("%d partidos pendientes de outcome (de %d totales en predictions.csv)", len(todo), len(pending))

    is_new_file = not config.OUTCOMES_LOG_PATH.exists()
    filled = not_final = failed = 0
    with open(config.OUTCOMES_LOG_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_OUTCOMES_FIELDS)
        if is_new_file:
            writer.writeheader()

        for game_pk, meta in todo.items():
            try:
                result = mlb_schedule.fetch_final_score(game_pk)
            except Exception as e:
                logger.warning("game_pk %s: fallo consulta de resultado tras reintentos (%s), anotado y saltado",
                                game_pk, e)
                store.append_failed_key(config.FAILED_KEYS_PATH, f"outcome:{game_pk}")
                failed += 1
                continue

            if result is None:
                not_final += 1
                continue

            writer.writerow({
                "date": meta["date"],
                "game_pk": game_pk,
                "home_team": meta["home_team"],
                "away_team": meta["away_team"],
                "home_score": result["home_score"],
                "away_score": result["away_score"],
                "home_win": result["home_win"],
                "backfilled_at": datetime.now(ZoneInfo(config.TZ)).isoformat(),
            })
            filled += 1

    logger.info("Backfill completo: filled=%d not_final=%d failed=%d -> %s",
                filled, not_final, failed, config.OUTCOMES_LOG_PATH)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    backfill()


if __name__ == "__main__":
    main()
