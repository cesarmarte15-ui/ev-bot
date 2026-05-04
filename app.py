import os
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from ev_engine import SPORTS, find_value_bets, build_game_predictions, debug_all_sports

load_dotenv()
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html", sports=SPORTS)

@app.route("/api/debug")
def api_debug():
    return jsonify(debug_all_sports())

@app.route("/api/picks")
def api_picks():
    selected_sports = request.args.getlist("sports") or ["MLB", "NBA", "NHL"]
    ev_min = float(request.args.get("ev_min", os.getenv("EV_MIN", "0.03")))
    edge_min = float(request.args.get("edge_min", os.getenv("EDGE_MIN", "0.03")))
    picks = find_value_bets(selected_sports, ev_min=ev_min, edge_min=edge_min)
    return jsonify({"count": len(picks), "picks": picks})

@app.route("/api/predictions")
def api_predictions():
    selected_sports = request.args.getlist("sports") or ["MLB", "NBA", "NHL"]
    predictions = build_game_predictions(selected_sports)
    return jsonify({"count": len(predictions), "predictions": predictions})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
