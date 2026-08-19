"""
Paths y constantes centrales del pipeline de modelo predictivo MLB.
Todo lo que corre bajo scripts/model/ importa de aca en vez de hardcodear
paths o parametros sueltos.
"""
from pathlib import Path

# --- Paths -------------------------------------------------------------
MODEL_ROOT   = Path(__file__).resolve().parent
DATA_DIR     = MODEL_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
RAW_DIR      = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
ARTIFACTS_DIR = MODEL_ROOT / "artifacts"
LOGS_DIR     = MODEL_ROOT / "logs"

MODEL_PATH        = ARTIFACTS_DIR / "model.joblib"
MODEL_METADATA_PATH = ARTIFACTS_DIR / "model_metadata.json"
METRICS_PATH       = ARTIFACTS_DIR / "metrics.json"
CALIBRATION_PATH   = ARTIFACTS_DIR / "calibration_report.csv"

PREDICTIONS_LOG_PATH = LOGS_DIR / "predictions.csv"
OUTCOMES_LOG_PATH    = LOGS_DIR / "outcomes.csv"

PARK_FACTORS_PATH      = REFERENCE_DIR / "park_factors.csv"
TEAM_NAME_ALIASES_PATH = REFERENCE_DIR / "team_name_aliases.csv"
PLAYER_ID_CROSSWALK_PATH = RAW_DIR / "player_id_crosswalk.csv"
FAILED_KEYS_PATH = RAW_DIR / "_failed_game_pks.txt"

# --- Temporadas / ventanas ----------------------------------------------
SEASONS: list[int] = [2023, 2024, 2025]
TRAIN_SEASONS: list[int] = [2023, 2024]
TEST_SEASON: int = 2025

STARTER_TRAILING_DAYS: int = 30       # ventana de FIP/ERA trailing del abridor
TEAM_FORM_TRAILING_GAMES: int = 10    # ventana de forma reciente del equipo (runs allowed/scored)
FANGRAPHS_SNAPSHOT_DAYS: int = 7      # cada cuantos dias se toma un snapshot ancla de FanGraphs

MIN_TRAILING_STARTS: int = 2          # partidos con menos arranques del abridor en la ventana se descartan
MIN_BULLPEN_TRAILING_IP: float = 5.0  # IP totales de bullpen minimas en la ventana; menos de eso, se descarta

# --- Red / timezone -------------------------------------------------------
TZ: str = "America/New_York"

MLB_STATS_API_BASE = "https://statsapi.mlb.com/api"

# App de EV Bot ya desplegada en produccion (para el matching de mercado en
# daily_score.py). No consume cuota de The Odds API: es un GET al endpoint
# publico de la app, igual que scripts/capture_snapshot.py.
EV_BOT_GAMES_URL = "https://ev-bot-nca1.onrender.com/api/games"

# --- Reintentos -------------------------------------------------------
RETRY_ATTEMPTS: int = 3
RETRY_BASE_DELAY_SECONDS: float = 5.0


def ensure_dirs() -> None:
    for d in (REFERENCE_DIR, RAW_DIR, PROCESSED_DIR, ARTIFACTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
