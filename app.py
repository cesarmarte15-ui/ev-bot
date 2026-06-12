import os
import logging
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from ev_engine import SPORTS, get_dashboard, debug_all_sports, clear_cache

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app_v3")

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", sports=SPORTS)


@app.route("/api/dashboard")
def api_dashboard():
    try:
        selected_sports = request.args.getlist("sports") or list(SPORTS.keys())
        force_refresh = request.args.get("refresh", "0") == "1"
        logger.info("Dashboard v8.3: sports=%s force=%s", selected_sports, force_refresh)
        data = get_dashboard(selected_sports, force_refresh=force_refresh)
        return jsonify(data)
    except Exception as e:
        logger.error("Dashboard error: %s", e, exc_info=True)
        return jsonify({
            "ok": False, "error": str(e),
            "sports": {}, "games": [], "gold_picks": [],
            "picks_del_dia": [], "player_props": [],
            "parlay_3": None, "parlay_6": None,
        }), 500


@app.route("/api/debug")
def api_debug():
    try:
        return jsonify(debug_all_sports())
    except Exception as e:
        logger.error("Debug error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/clear-cache")
def api_clear_cache():
    clear_cache()
    return jsonify({"ok": True, "message": "Cache limpiado"})


@app.route("/health")
def health():
    return jsonify({"ok": True, "version": "8.3"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
