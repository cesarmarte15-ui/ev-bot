"""
Captura periodica de /api/games en produccion para acumular historial de
picks y poder analizar, con datos reales de varios dias, si los umbrales
de eficiencia (Prob/Val) son razonables o demasiado estrictos.

No toca ev_engine.py ni app.py: es un cliente HTTP externo, solo lee el
endpoint publico. Pensado para correr via Task Scheduler cada pocas horas.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://ev-bot-nca1.onrender.com/api/games"
OUT_DIR = Path(__file__).resolve().parent.parent / "snapshots"


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    try:
        with urllib.request.urlopen(URL, timeout=30) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"ERROR fetching {URL}: {e}", file=sys.stderr)
        return 1

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_file = OUT_DIR / f"{ts}.json"
    out_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    n_games = len(data.get("all_games", []))
    n_avoid = len(data.get("avoid", []))
    print(f"Saved {out_file.name}: ok={data.get('ok')} games={n_games} avoid_signals={n_avoid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
