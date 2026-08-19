"""
CLI: python -m scripts.model.train_model [--train-seasons 2023 2024] [--test-season 2025]

Entrena el modelo de probabilidad de victoria del home team sobre el
dataset compilado por build_training_data.py.

Split temporal, no aleatorio: TRAIN_SEASONS entrena, TEST_SEASON evalua
(nunca al reves) — asi la metrica refleja lo que el modelo veria en
produccion (temporada futura nunca vista), no un shuffle optimista entre
temporadas.

Modelo: StandardScaler + LogisticRegression en un Pipeline. Se eligio
sobre alternativas mas "potentes" (GBM, random forest) porque
daily_score.py necesita probabilidades bien calibradas para el calculo de
EV, no solo separacion — LogisticRegression las da out of the box con
pocos datos (~3400 filas de train) y sin tuning; ver calibration_report.csv
para verificar el supuesto antes de confiar en el modelo para EV real.

Outputs (todos bajo scripts/model/artifacts/, gitignored):
- model.joblib: el Pipeline entrenado completo.
- model_metadata.json: feature_names (orden exacto que espera el Pipeline),
  train_seasons, test_season, n_train, n_test, trained_at, sklearn_version.
- metrics.json: accuracy, log_loss, brier_score, roc_auc en TEST_SEASON.
- calibration_report.csv: bins de probabilidad predicha vs win rate real
  (sklearn.calibration.calibration_curve).
"""
import argparse
import json
import logging
import platform
from datetime import datetime, timezone

import joblib
import pandas as pd
import sklearn
from sklearn.calibration import calibration_curve
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import build_training_data, config, features

logger = logging.getLogger("model.train_model")


def _load_dataset(dataset_seasons: list[int]) -> pd.DataFrame:
    path = build_training_data.dataset_path(dataset_seasons)
    if not path.exists():
        raise FileNotFoundError(
            f"No existe {path} — corre build_training_data.py primero "
            f"(--seasons {' '.join(map(str, dataset_seasons))})."
        )
    return pd.read_parquet(path)


def _split(df: pd.DataFrame, train_seasons: list[int], test_season: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    train_df = df[df["season"].isin(train_seasons)]
    test_df = df[df["season"] == test_season]
    if train_df.empty:
        raise RuntimeError(f"0 filas de entrenamiento para temporadas {train_seasons}")
    if test_df.empty:
        raise RuntimeError(f"0 filas de test para temporada {test_season}")
    return train_df, test_df


def train(train_seasons: list[int], test_season: int, dataset_seasons: list[int]) -> None:
    config.ensure_dirs()
    df = _load_dataset(dataset_seasons)
    train_df, test_df = _split(df, train_seasons, test_season)
    logger.info("Train: %d filas (temporadas %s) | Test: %d filas (temporada %d)",
                len(train_df), train_seasons, len(test_df), test_season)

    X_train, y_train = train_df[features.FEATURE_NAMES], train_df["home_win"]
    X_test, y_test = test_df[features.FEATURE_NAMES], test_df["home_win"]

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000)),
    ])
    pipeline.fit(X_train, y_train)

    proba_test = pipeline.predict_proba(X_test)[:, 1]
    pred_test = (proba_test >= 0.5).astype(int)

    metrics = {
        "accuracy": accuracy_score(y_test, pred_test),
        "log_loss": log_loss(y_test, proba_test),
        "brier_score": brier_score_loss(y_test, proba_test),
        "roc_auc": roc_auc_score(y_test, proba_test),
        "home_win_rate_test": float(y_test.mean()),  # baseline naive: "siempre gana el home"
        "n_train": len(train_df),
        "n_test": len(test_df),
    }
    logger.info("Metricas en temporada %d:\n%s", test_season, json.dumps(metrics, indent=2))

    frac_pos, mean_pred = calibration_curve(y_test, proba_test, n_bins=10, strategy="quantile")
    pd.DataFrame({
        "mean_predicted_prob": mean_pred,
        "fraction_of_positives": frac_pos,
    }).to_csv(config.CALIBRATION_PATH, index=False)

    joblib.dump(pipeline, config.MODEL_PATH)

    metadata = {
        "feature_names": features.FEATURE_NAMES,
        "train_seasons": train_seasons,
        "test_season": test_season,
        "n_train": len(train_df),
        "n_test": len(test_df),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "sklearn_version": sklearn.__version__,
        "python_version": platform.python_version(),
        "model": "StandardScaler + LogisticRegression",
    }
    with open(config.MODEL_METADATA_PATH, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    with open(config.METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    logger.info("Modelo -> %s | metadata -> %s | metrics -> %s | calibracion -> %s",
                config.MODEL_PATH, config.MODEL_METADATA_PATH, config.METRICS_PATH, config.CALIBRATION_PATH)


def main():
    parser = argparse.ArgumentParser(description="Entrena el modelo de probabilidad de victoria MLB")
    parser.add_argument("--train-seasons", type=int, nargs="+", default=config.TRAIN_SEASONS)
    parser.add_argument("--test-season", type=int, default=config.TEST_SEASON)
    parser.add_argument("--dataset-seasons", type=int, nargs="+", default=config.SEASONS,
                         help="Temporadas del parquet compilado a cargar (debe cubrir train+test).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    train(args.train_seasons, args.test_season, args.dataset_seasons)


if __name__ == "__main__":
    main()
