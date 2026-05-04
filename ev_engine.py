import os
import time
import requests
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

CACHE = {}
CACHE_TTL = int(os.getenv("CACHE_TTL", "900"))

def clear_cache():
    CACHE.clear()

def api_key_status():
    if not API_KEY:
        return "missing"
    if len(API_KEY) < 10:
        return "too_short"
    return "present"

def american_to_decimal(odds):
    if odds is None:
        return None
    return 1 + odds / 100 if odds > 0 else 1 + 100 / abs(odds)

def implied_probability_american(odds):
    if odds is None:
        return None
    return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)

def normalize_market_probabilities(outcomes):
    probs = []
    for out in outcomes:
        p = implied_probability_american(out.get("price"))
        if p is not None:
            probs.append((out, p))
    total = sum(p for _, p in probs)
    if total <= 0:
        return []
    return [(out, p / total) for out, p in probs]

def calculate_ev(true_prob, decimal_odds):
    if true_prob is None or decimal_odds is None:
        return None
    return true_prob * decimal_odds - 1

def fetch_odds(sport_key, force_refresh=False):
    if not API_KEY or API_KEY == "pon_tu_api_key_aqui":
        raise RuntimeError("Falta ODDS_API_KEY en Render Environment")

    cache_key = f"{sport_key}:{REGIONS}:{MARKETS}:{ODDS_FORMAT}"
    now = time.time()

    if not force_refresh and cache_key in CACHE:
        cached_time, cached_data = CACHE[cache_key]
        if now - cached_time < CACHE_TTL:
            return cached_data, True, int(CACHE_TTL - (now - cached_time))

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": "iso",
    }
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"The Odds API error {r.status_code}: {detail}")

    data = r.json()
    CACHE[cache_key] = (now, data)
    return data, False, CACHE_TTL

def find_best_price_for_same_outcome(game, outcome_name):
    best = None
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                if outcome.get("name") == outcome_name:
                    price = outcome.get("price")
                    if price is None:
                        continue
                    dec = american_to_decimal(price)
                    if best is None or dec > best["decimal_odds"]:
                        best = {"bookmaker": bookmaker.get("title", "Unknown"), "american_odds": price, "decimal_odds": dec}
    return best

def consensus_market_outcomes(game):
    accumulator = {}
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome, fair_prob in normalize_market_probabilities(market.get("outcomes", [])):
                name = outcome.get("name")
                item = accumulator.setdefault(name, {"name": name, "fair_probs": [], "bookmakers": set()})
                item["fair_probs"].append(fair_prob)
                item["bookmakers"].add(bookmaker.get("title", "Unknown"))

    results = []
    for name, item in accumulator.items():
        best = find_best_price_for_same_outcome(game, name)
        if not best or not item["fair_probs"]:
            continue
        avg_prob = sum(item["fair_probs"]) / len(item["fair_probs"])
        implied = implied_probability_american(best["american_odds"])
        dec = best["decimal_odds"]
        ev = calculate_ev(avg_prob, dec)
        edge = avg_prob - implied
        results.append({
            "selection": name,
            "market": "Moneyline",
            "probability": round(avg_prob * 100, 1),
            "book_count": len(item["bookmakers"]),
            "odds": best["american_odds"],
            "decimal_odds": round(dec, 3),
            "bookmaker": best["bookmaker"],
            "implied_probability": round(implied * 100, 1),
            "edge": round(edge * 100, 1),
            "ev": round(ev * 100, 1),
        })

    results.sort(key=lambda x: (x.get("ev") or -999, x.get("probability") or 0), reverse=True)
    return results

def confidence_label(prob, ev, edge, books):
    score = (prob or 0) + (ev or 0) + (edge or 0) + min(books or 0, 6)
    if score >= 68:
        return "Alta"
    if score >= 58:
        return "Media"
    return "Baja"

def game_prediction(game, sport_label):
    home = game.get("home_team")
    away = game.get("away_team")
    options = consensus_market_outcomes(game)
    for p in options:
        p["confidence"] = confidence_label(p["probability"], p["ev"], p["edge"], p["book_count"])
    best = options[0] if options else None
    return {
        "sport": sport_label,
        "game": f"{away} vs {home}",
        "home_team": home,
        "away_team": away,
        "start_time": game.get("commence_time"),
        "winner": best,
        "best_bet": best,
        "moneyline_options": options,
        "note": "Modo Pro Controlado: solo Moneyline para ahorrar créditos. Spread/Total se agregan con más créditos."
    }

def get_dashboard(selected_sports, force_refresh=False):
    dashboard = {
        "mode": "Pro Controlado",
        "credit_saving": True,
        "cache_ttl_seconds": CACHE_TTL,
        "sports": {},
        "games": [],
        "best_picks": [],
        "warnings": []
    }

    for sport_label in selected_sports:
        sport_key = SPORTS.get(sport_label)
        if not sport_key:
            continue
        try:
            games, from_cache, ttl_left = fetch_odds(sport_key, force_refresh=force_refresh)
            dashboard["sports"][sport_label] = {"ok": True, "sport_key": sport_key, "games_count": len(games), "from_cache": from_cache, "cache_seconds_left": ttl_left}
            for game in games:
                pred = game_prediction(game, sport_label)
                dashboard["games"].append(pred)
                if pred["best_bet"]:
                    pick = pred["best_bet"].copy()
                    pick.update({"sport": sport_label, "game": pred["game"], "start_time": pred["start_time"]})
                    dashboard["best_picks"].append(pick)
        except Exception as e:
            dashboard["sports"][sport_label] = {"ok": False, "sport_key": sport_key, "games_count": 0, "error": str(e)}
            dashboard["warnings"].append(f"{sport_label}: {str(e)}")

    dashboard["best_picks"].sort(key=lambda x: ((x.get("ev") or -999), (x.get("probability") or 0)), reverse=True)
    dashboard["best_picks"] = dashboard["best_picks"][:12]
    return dashboard

def find_value_bets(selected_sports, ev_min=0.0, edge_min=0.0, force_refresh=False):
    dashboard = get_dashboard(selected_sports, force_refresh=force_refresh)
    picks = []
    for pick in dashboard["best_picks"]:
        ev = (pick.get("ev") or 0) / 100
        edge = (pick.get("edge") or 0) / 100
        if ev >= ev_min and edge >= edge_min:
            picks.append(pick)
    return picks

def debug_all_sports():
    result = {"api_key": api_key_status(), "regions": REGIONS, "markets": MARKETS, "odds_format": ODDS_FORMAT, "cache_ttl_seconds": CACHE_TTL, "sports": {}}
    for label, key in SPORTS.items():
        try:
            games, from_cache, ttl_left = fetch_odds(key)
            result["sports"][label] = {
                "ok": True,
                "sport_key": key,
                "games_count": len(games),
                "from_cache": from_cache,
                "cache_seconds_left": ttl_left,
                "sample_games": [{"home_team": g.get("home_team"), "away_team": g.get("away_team"), "commence_time": g.get("commence_time"), "bookmakers_count": len(g.get("bookmakers", []))} for g in games[:5]]
            }
        except Exception as e:
            result["sports"][label] = {"ok": False, "sport_key": key, "games_count": 0, "error": str(e)}
    return result
