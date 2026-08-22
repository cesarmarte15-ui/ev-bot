import os
import logging
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from ev_engine import SPORTS, SPORTS_EFFICIENCY, get_dashboard, debug_all_sports, clear_cache

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app")

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html", sports=SPORTS,
                           sports_efficiency=SPORTS_EFFICIENCY)

@app.route("/api/dashboard")
def api_dashboard():
    try:
        selected = request.args.getlist("sports") or ["ALL"]
        force    = request.args.get("refresh", "0") == "1"
        logger.info("Dashboard v8.5: sports=%s force=%s", selected, force)
        data = get_dashboard(selected, force_refresh=force)
        return jsonify(data)
    except Exception as e:
        logger.error("Dashboard error: %s", e, exc_info=True)
        return jsonify({
            "ok": False, "error": str(e),
            "sports": {}, "games": [],
            "efficiency_green": [], "efficiency_blue": [], "efficiency_yellow": [],
            "green": [], "blue": [], "yellow": [], "red": [],
            "kelly_ranking": [], "kelly_near_miss": None,
            "tickets": [],
        }), 500

@app.route("/api/efficiency")
def api_efficiency():
    """Solo picks de eficiencia MLB/NBA/NHL."""
    try:
        force = request.args.get("refresh", "0") == "1"
        data  = get_dashboard(["ALL"], force_refresh=force)
        return jsonify({
            "ok":                True,
            "efficiency_green":  data.get("efficiency_green", []),
            "efficiency_blue":   data.get("efficiency_blue", []),
            "efficiency_yellow": data.get("efficiency_yellow", []),
            "ticket_efficiency": data.get("ticket_efficiency"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/games")
def api_games():
    """Todos los juegos con evaluación ML/Spread/Total."""
    try:
        selected = request.args.getlist("sports") or ["ALL"]
        force    = request.args.get("refresh", "0") == "1"
        data     = get_dashboard(selected, force_refresh=force)
        return jsonify({
            "ok":        True,
            "all_games": data.get("all_games", []),
            "avoid":     data.get("avoid", []),
        })
    except Exception as e:
        logger.error("Games error: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/debug")
def api_debug():
    try:
        return jsonify(debug_all_sports())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/clear-cache")
def api_clear_cache():
    clear_cache()
    return jsonify({"ok": True, "message": "Cache limpiado"})

@app.route("/health")
def health():
    return jsonify({"ok": True, "version": "8.6"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
