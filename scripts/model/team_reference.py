"""
Datos de referencia estaticos: mapeo de team_id (MLB Stats API) a
abreviatura de Baseball-Reference (la que espera pybaseball.schedule_and_record)
y park factors por venue_id.

TEAM_ID_TO_BBREF_ABBR se construyo cruzando el endpoint real
/api/v1/teams?sportId=1 (que da team_id + venue_id + venue_name, usados
para park_factors.csv) contra pybaseball.schedule_and_record, probando
CADA una de las 30 abreviaturas contra la API real de Baseball-Reference
antes de commitear esta tabla — no es una lista de memoria. Los casos
ambiguos por ciudad compartida (Athletics, Chicago White Sox, Kansas City,
San Diego, Tampa Bay, Washington, San Francisco) se verificaron primero
por ser los de mayor riesgo de error silencioso.
"""
import csv
import logging

from . import config

logger = logging.getLogger("model.team_reference")

# team_id (MLB Stats API) -> abreviatura de Baseball-Reference
TEAM_ID_TO_BBREF_ABBR: dict[int, str] = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC",
    113: "CIN", 114: "CLE", 115: "COL", 116: "DET", 117: "HOU",
    118: "KCR", 119: "LAD", 120: "WSN", 121: "NYM", 133: "ATH",
    134: "PIT", 135: "SDP", 136: "SEA", 137: "SFG", 138: "STL",
    139: "TBR", 140: "TEX", 141: "TOR", 142: "MIN", 143: "PHI",
    144: "ATL", 145: "CHW", 146: "MIA", 147: "NYY", 158: "MIL",
}


def bbref_abbr(team_id: int, season: int) -> str:
    """
    team_id 133 (Athletics) es season-dependent en Baseball-Reference:
    verificado a mano contra la API real que "OAK" es la unica abreviatura
    valida para 2023-2024 ("ATH" falla con "Data cannot be retrieved for
    this team/year combo"), mientras que "ATH" (y tambien "OAK", parece
    redirigir) funciona para 2025+ tras la mudanza/rebranding a
    Sacramento. Se descubrio en vivo durante el pull historico completo
    (el error generico de pybaseball no menciona el equipo, asi que
    quedo documentado aca para que no se repita la investigacion).
    """
    if team_id == 133:
        return "OAK" if season <= 2024 else "ATH"
    try:
        return TEAM_ID_TO_BBREF_ABBR[team_id]
    except KeyError:
        raise ValueError(f"team_id {team_id} no esta en TEAM_ID_TO_BBREF_ABBR (equipo nuevo/expansion?)")


def load_park_factors() -> dict[int, float]:
    """venue_id -> multiplicador de park factor (1.00 = neutral). Valores
    aproximados de reputacion de largo plazo (ver source_note por fila en
    el CSV), pensados como proxy MVP — no un pull exacto de guts de
    FanGraphs. Refinar si el modelo justifica la inversion.

    LIMITACION CONOCIDA: las 30 filas "estables" se armaron cruzando
    /api/v1/teams (sede ACTUAL de cada equipo), que no refleja reubicaciones
    de una temporada especifica. Ya se detectaron y agregaron a mano dos
    casos reales dentro de la ventana 2023-2025: Rays jugando en George M.
    Steinbrenner Field en 2025 (Tropicana Field danado por el huracan
    Milton) y Athletics en Sutter Health Park en 2025 (mudanza a
    Sacramento). Si build_training_data.py loguea un warning de venue_id
    desconocido durante el pull completo, es la señal de que hay que
    agregar otro caso asi — el pipeline no se rompe (usa el default
    neutral 1.0), pero pierde precision en esa fila."""
    if not config.PARK_FACTORS_PATH.exists():
        raise FileNotFoundError(f"No existe {config.PARK_FACTORS_PATH}")
    out: dict[int, float] = {}
    with open(config.PARK_FACTORS_PATH, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[int(row["venue_id"])] = float(row["park_factor"])
    return out
