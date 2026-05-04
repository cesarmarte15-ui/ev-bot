import os
import math
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY", "")

SPORTS = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl"
}


REGIONS = "us"
MARKETS = "h2h"
ODDS_FORMAT = "american"

def american_to_decimal(odds):
    if odds is None:
        return None
    if odds > 0:
        return 1 + odds / 100
    return 1 + 100 / abs(odds)

def implied_probability_american(odds):
    if odds is None:
        return None
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)

def normalize_market_probabilities(outcomes):
    probs = []
    for out in outcomes:
        odds = out.get("price")
        p = implied_probability_american(odds)
        if p is not None:
            probs.append((out, p))
    total = sum(p for _, p in probs)
    if total <= 0:
        return []
    return [(out, p / total) for out, p in probs]

def calculate_ev(true_prob, decimal_odds):
    if true_prob is None or decimal_odds is None:
        return None
    return (true_prob * decimal_odds) - 1

def fetch_odds(sport_key):
    if not API_KEY or API_KEY == "pon_tu_api_key_aqui":
        raise RuntimeError("Falta ODDS_API_KEY en el archivo .env")

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": "iso",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def find_best_price_for_same_outcome(game, market_key, outcome_name, point=None):
    best = None
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []):
                same_name = outcome.get("name") == outcome_name
                same_point = (point is None or outcome.get("point") == point)
                if same_name and same_point:
                    price = outcome.get("price")
                    if price is None:
                        continue
                    dec = american_to_decimal(price)
                    if best is None or dec > best["decimal_odds"]:
                        best = {
                            "bookmaker": bookmaker.get("title", "Unknown"),
                            "american_odds": price,
                            "decimal_odds": dec,
                        }
    return best

def find_value_bets(selected_sports, ev_min=0.03, edge_min=0.03):
    all_picks = []

    for sport_label in selected_sports:
        sport_key = SPORTS.get(sport_label, sport_label)
        try:
            games = fetch_odds(sport_key)
        except Exception as e:
            all_picks.append({
                "sport": sport_label,
                "error": str(e)
            })
            continue

        for game in games:
            teams = f"{game.get('away_team')} vs {game.get('home_team')}"
            commence_time = game.get("commence_time", "")

            # Usamos el consenso de cada sportsbook para estimar probabilidad justa sin vig.
            for bookmaker in game.get("bookmakers", []):
                book_title = bookmaker.get("title", "Unknown")
                for market in bookmaker.get("markets", []):
                    market_key = market.get("key")
                    outcomes = market.get("outcomes", [])

                    normalized = normalize_market_probabilities(outcomes)
                    if len(normalized) < 2:
                        continue

                    for outcome, fair_prob in normalized:
                        outcome_name = outcome.get("name")
                        point = outcome.get("point")
                        best_price = find_best_price_for_same_outcome(
                            game, market_key, outcome_name, point
                        )
                        if not best_price:
                            continue

                        market_price = outcome.get("price")
                        market_prob = implied_probability_american(best_price["american_odds"])
                        decimal_odds = best_price["decimal_odds"]
                        ev = calculate_ev(fair_prob, decimal_odds)
                        edge = fair_prob - market_prob

                        if ev is None or edge is None:
                            continue

                        if ev >= ev_min and edge >= edge_min:
                            all_picks.append({
                                "sport": sport_label,
                                "game": teams,
                                "start_time": commence_time,
                                "market": market_key,
                                "selection": outcome_name,
                                "point": point,
                                "bookmaker": best_price["bookmaker"],
                                "american_odds": best_price["american_odds"],
                                "decimal_odds": round(decimal_odds, 3),
                                "fair_probability": round(fair_prob * 100, 2),
                                "implied_probability": round(market_prob * 100, 2),
                                "edge": round(edge * 100, 2),
                                "ev": round(ev * 100, 2),
                                "source_book": book_title,
                            })

    all_picks = [p for p in all_picks if "error" not in p]
    all_picks.sort(key=lambda x: (x.get("ev", 0), x.get("edge", 0)), reverse=True)
    return all_picks[:100]


def debug_all_sports():
    result = {
        "api_key": "OK" if API_KEY else "MISSING",
        "sports": {}
    }

    for label, key in SPORTS.items():
        try:
            games = fetch_odds(key)
            result["sports"][label] = {
                "games_count": len(games),
                "ok": True
            }
        except Exception as e:
            result["sports"][label] = {
                "games_count": 0,
                "ok": False,
                "error": str(e)
            }

    return result
