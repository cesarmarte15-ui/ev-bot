import os
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from ev_engine import find_value_bets, SPORTS

load_dotenv()

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html", sports=SPORTS)

@app.route("/api/picks")
def api_picks():
    selected_sports = request.args.getlist("sports")
    if not selected_sports:
        selected_sports = list(SPORTS.keys())

    try:
        ev_min = float(request.args.get("ev_min", os.getenv("EV_MIN", "0.03")))
        edge_min = float(request.args.get("edge_min", os.getenv("EDGE_MIN", "0.03")))
    except ValueError:
        ev_min = 0.03
        edge_min = 0.03

    picks = find_value_bets(selected_sports, ev_min=ev_min, edge_min=edge_min)
    return jsonify({"count": len(picks), "picks": picks})

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
