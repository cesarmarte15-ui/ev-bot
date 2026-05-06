import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
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

LOCAL_TZ = os.getenv("LOCAL_TZ", "America/New_York")
ONLY_TODAY = os.getenv("ONLY_TODAY", "1") == "1"

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

def is_game_today(game):
    if not ONLY_TODAY:
        return True
    start = game.get("commence_time")
    if not start:
        return False
    try:
        dt_utc = datetime.fromisoformat(start.replace("Z", "+00:00"))
        local_zone = ZoneInfo(LOCAL_TZ)
        return dt_utc.astimezone(local_zone).date() == datetime.now(local_zone).date()
    except Exception:
        return False

def classify_pick(prob, ev, edge, odds, books):
    prob = prob or 0
    ev = ev or 0
    edge = edge or 0
    books = books or 0

    payout_high = odds is not None and odds >= 300
    payout_extreme = odds is not None and odds >= 700
    heavy_favorite = odds is not None and odds <= -250

    score = 0
    score += min(prob, 70) * 0.55
    score += min(max(edge, 0), 10) * 2.6
    score += min(max(ev, 0), 20) * 0.75
    score += min(books, 8) * 0.9

    if payout_high:
        score -= 8
    if payout_extreme:
        score -= 18
    if prob < 20:
        score -= 15
    elif prob < 35:
        score -= 7
    if heavy_favorite and edge < 3:
        score -= 8
    if ev < 0 or edge < 0:
        score -= 30

    if ev <= 0 or edge <= 0:
        return {"action":"EVITAR","category":"Evitar","confidence":"No jugar","risk":"Alto","stake":"0u","score":round(score,1),"playable":False,"reason":"La línea no tiene valor positivo contra la casa."}
    if payout_extreme or prob < 20:
        return {"action":"PAGO ALTO","category":"Pago Alto","confidence":"Baja","risk":"Muy alto","stake":"0.10u-0.25u","score":round(score,1),"playable":True,"reason":"Paga alto y tiene valor, pero la probabilidad de ganar es baja."}
    if payout_high or prob < 35:
        return {"action":"PAGO ALTO","category":"Pago Alto","confidence":"Baja/Media","risk":"Alto","stake":"0.25u-0.50u","score":round(score,1),"playable":True,"reason":"Underdog con valor. Jugar pequeño si decides tomar riesgo."}
    if prob >= 55 and edge >= 3 and ev >= 3:
        return {"action":"JUGAR","category":"Jugar","confidence":"Alta","risk":"Medio/Bajo","stake":"1u","score":round(score,1),"playable":True,"reason":"Probabilidad, valor y ventaja están alineados."}
    if prob >= 40 and edge >= 2 and ev >= 2:
        return {"action":"JUGAR","category":"Jugar","confidence":"Media","risk":"Medio","stake":"0.50u-0.75u","score":round(score,1),"playable":True,"reason":"Tiene valor positivo, pero no es pick fuerte."}
    return {"action":"OBSERVAR","category":"Observar","confidence":"Baja","risk":"Medio/Alto","stake":"0.25u","score":round(score,1),"playable":True,"reason":"Valor leve. Mejor esperar o jugar mínimo."}

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
        ev = calculate_ev(avg_prob, best["decimal_odds"])
        edge = avg_prob - implied
        prob_pct = round(avg_prob * 100, 1)
        ev_pct = round(ev * 100, 1)
        edge_pct = round(edge * 100, 1)
        rating = classify_pick(prob_pct, ev_pct, edge_pct, best["american_odds"], len(item["bookmakers"]))

        results.append({
            "selection": name,
            "market": "Moneyline",
            "probability": prob_pct,
            "book_count": len(item["bookmakers"]),
            "odds": best["american_odds"],
            "decimal_odds": round(best["decimal_odds"], 3),
            "bookmaker": best["bookmaker"],
            "implied_probability": round(implied * 100, 1),
            "edge": edge_pct,
            "ev": ev_pct,
            "action": rating["action"],
            "category": rating["category"],
            "confidence": rating["confidence"],
            "risk": rating["risk"],
            "stake": rating["stake"],
            "rating_score": rating["score"],
            "reason": rating["reason"],
            "playable": rating["playable"]
        })
    results.sort(key=lambda x: (x.get("rating_score") or -999, x.get("edge") or -999, x.get("ev") or -999), reverse=True)
    return results

def game_prediction(game, sport_label):
    home = game.get("home_team")
    away = game.get("away_team")
    options = consensus_market_outcomes(game)
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
        "note": "ML de hoy. Señales limpias: Jugar, Pago Alto, Observar o Evitar."
    }

def get_dashboard(selected_sports, force_refresh=False):
    dashboard = {"mode":"Pro Controlado v4","credit_saving":True,"cache_ttl_seconds":CACHE_TTL,"only_today":ONLY_TODAY,"timezone":LOCAL_TZ,"sports":{},"games":[],"top_picks":[],"high_payout":[],"avoid":[],"all_signals":[],"warnings":[]}

    for sport_label in selected_sports:
        sport_key = SPORTS.get(sport_label)
        if not sport_key:
            continue
        try:
            games, from_cache, ttl_left = fetch_odds(sport_key, force_refresh=force_refresh)
            total_games = len(games)
            games = [g for g in games if is_game_today(g)]
            dashboard["sports"][sport_label] = {"ok":True,"sport_key":sport_key,"games_count":len(games),"total_api_games":total_games,"today_only":ONLY_TODAY,"timezone":LOCAL_TZ,"from_cache":from_cache,"cache_seconds_left":ttl_left}
            for game in games:
                pred = game_prediction(game, sport_label)
                dashboard["games"].append(pred)
                if pred["best_bet"]:
                    pick = pred["best_bet"].copy()
                    pick.update({"sport":sport_label,"game":pred["game"],"start_time":pred["start_time"]})
                    dashboard["all_signals"].append(pick)
                    if pick["action"] == "JUGAR":
                        dashboard["top_picks"].append(pick)
                    elif pick["action"] == "PAGO ALTO":
                        dashboard["high_payout"].append(pick)
                    elif pick["action"] == "EVITAR":
                        dashboard["avoid"].append(pick)
        except Exception as e:
            dashboard["sports"][sport_label] = {"ok":False,"sport_key":sport_key,"games_count":0,"error":str(e)}
            dashboard["warnings"].append(f"{sport_label}: {str(e)}")

    sorter = lambda x: ((x.get("rating_score") or -999), (x.get("probability") or 0), (x.get("edge") or -999))
    dashboard["top_picks"].sort(key=sorter, reverse=True)
    dashboard["high_payout"].sort(key=sorter, reverse=True)
    dashboard["avoid"].sort(key=lambda x: x.get("probability") or 0, reverse=True)
    dashboard["all_signals"].sort(key=sorter, reverse=True)
    dashboard["top_picks"] = dashboard["top_picks"][:8]
    dashboard["high_payout"] = dashboard["high_payout"][:8]
    dashboard["avoid"] = dashboard["avoid"][:12]
    return dashboard

def find_value_bets(selected_sports, ev_min=0.0, edge_min=0.0, force_refresh=False):
    dashboard = get_dashboard(selected_sports, force_refresh=force_refresh)
    picks = []
    for group in ["top_picks", "high_payout"]:
        for pick in dashboard[group]:
            ev = (pick.get("ev") or 0) / 100
            edge = (pick.get("edge") or 0) / 100
            if ev >= ev_min and edge >= edge_min:
                picks.append(pick)
    return picks

def debug_all_sports():
    result = {"api_key":api_key_status(),"regions":REGIONS,"markets":MARKETS,"odds_format":ODDS_FORMAT,"cache_ttl_seconds":CACHE_TTL,"only_today":ONLY_TODAY,"timezone":LOCAL_TZ,"sports":{}}
    for label, key in SPORTS.items():
        try:
            games, from_cache, ttl_left = fetch_odds(key)
            total_games = len(games)
            games = [g for g in games if is_game_today(g)]
            result["sports"][label] = {"ok":True,"sport_key":key,"games_count":len(games),"total_api_games":total_games,"today_only":ONLY_TODAY,"timezone":LOCAL_TZ,"from_cache":from_cache,"cache_seconds_left":ttl_left,"sample_games":[{"home_team":g.get("home_team"),"away_team":g.get("away_team"),"commence_time":g.get("commence_time"),"bookmakers_count":len(g.get("bookmakers", []))} for g in games[:5]]}
        except Exception as e:
            result["sports"][label] = {"ok":False,"sport_key":key,"games_count":0,"error":str(e)}
    return result
