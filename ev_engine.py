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

REGIONS = os.getenv("REGIONS", "us")
MARKETS = os.getenv("MARKETS", "h2h,spreads,totals")
ODDS_FORMAT = "american"

LOCAL_TZ = os.getenv("LOCAL_TZ", "America/New_York")
ONLY_TODAY = os.getenv("ONLY_TODAY", "1") == "1"

CACHE = {}
CACHE_TTL = int(os.getenv("CACHE_TTL", "900"))

def clamp(value, low, high):
    try:
        return max(low, min(high, value))
    except Exception:
        return low


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

def get_market_outcomes(game, market_key):
    rows = []
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []):
                rows.append({
                    "bookmaker": bookmaker.get("title", "Unknown"),
                    "market": market_key,
                    "name": outcome.get("name"),
                    "price": outcome.get("price"),
                    "point": outcome.get("point"),
                    "description": outcome.get("description"),
                })
    return rows

def best_price_same(outcomes, name, point=None):
    best = None
    for out in outcomes:
        same_name = out.get("name") == name
        same_point = point is None or out.get("point") == point
        if not same_name or not same_point:
            continue
        price = out.get("price")
        if price is None:
            continue
        dec = american_to_decimal(price)
        if best is None or dec > best["decimal_odds"]:
            best = {
                "bookmaker": out.get("bookmaker", "Unknown"),
                "american_odds": price,
                "decimal_odds": dec,
                "point": out.get("point"),
            }
    return best


def best_price_filtered(outcomes, name, fair_prob_decimal, point=None):
    """
    Evita cuotas outlier absurdas.
    Ejemplo: favorito 61% con odds +1060.
    """
    candidates = []
    for out in outcomes:
        if out.get("name") != name:
            continue
        if point is not None and out.get("point") != point:
            continue
        price = out.get("price")
        if price is None:
            continue
        implied = implied_probability_american(price)
        dec = american_to_decimal(price)
        if implied is None or dec is None:
            continue
        diff = abs(implied - fair_prob_decimal)
        candidates.append({
            "bookmaker": out.get("bookmaker", "Unknown"),
            "american_odds": price,
            "decimal_odds": dec,
            "point": out.get("point"),
            "implied": implied,
            "diff": diff
        })

    if not candidates:
        return None

    # Filtro fuerte: elimina líneas que no tienen sentido contra el consenso.
    sane = [c for c in candidates if c["diff"] <= 0.25]

    # Si todas son raras, usa la más cercana al consenso, no la que paga más.
    if not sane:
        return sorted(candidates, key=lambda c: c["diff"])[0]

    # Entre líneas sanas, usa la mejor cuota.
    return sorted(sane, key=lambda c: c["decimal_odds"], reverse=True)[0]


def safe_ev_edge(fair_prob_decimal, odds):
    dec = american_to_decimal(odds)
    implied = implied_probability_american(odds)
    if dec is None or implied is None:
        return None, None
    ev = calculate_ev(fair_prob_decimal, dec)
    edge = fair_prob_decimal - implied
    return round(clamp(ev * 100, -25, 25), 1), round(clamp(edge * 100, -20, 20), 1)


def market_probability_from_consensus(outcomes, name, point=None):
    """
    Calcula probabilidad realista usando consenso sportsbook.
    Corrige el bug de probabilidades absurdas 7%-14%.
    """
    by_book = []
    for out in outcomes:
        same_name = out.get("name") == name
        same_point = point is None or out.get("point") == point
        price = out.get("price")
        if not same_name or not same_point or price is None:
            continue
        p = implied_probability_american(price)
        if p is not None:
            by_book.append(p)

    if not by_book:
        return None

    avg = sum(by_book) / len(by_book)
    return avg

def no_vig_h2h_probabilities(outcomes):
    """
    Agrupa outcomes h2h por casa y remueve vig por bookmaker.
    Luego promedia por equipo.
    """
    by_book = {}
    for out in outcomes:
        book = out.get("bookmaker", "Unknown")
        name = out.get("name")
        price = out.get("price")
        if name is None or price is None:
            continue
        p = implied_probability_american(price)
        if p is None:
            continue
        by_book.setdefault(book, []).append((name, p))

    team_probs = {}
    team_counts = {}

    for book, items in by_book.items():
        total = sum(p for _, p in items)
        if total <= 0:
            continue
        for name, p in items:
            fair = p / total
            team_probs[name] = team_probs.get(name, 0) + fair
            team_counts[name] = team_counts.get(name, 0) + 1

    results = {}
    for name, total_p in team_probs.items():
        count = team_counts.get(name, 1)
        results[name] = total_p / count

    return results, team_counts

def smooth_probability(prob_pct, odds, book_count, ev_pct=0, edge_pct=0):
    prob_pct = clamp(prob_pct or 0, 1, 85)
    ev_pct = clamp(ev_pct or 0, -10, 10)
    edge_pct = clamp(edge_pct or 0, -8, 8)
    book_count = book_count or 0

    confidence_bonus = min(book_count * 0.6, 5)
    price_adjustment = 0

    if odds is not None:
        if odds <= -1000:
            price_adjustment -= 18
        elif odds <= -400:
            price_adjustment -= 10
        elif odds <= -250:
            price_adjustment -= 5
        elif odds <= -150:
            price_adjustment += 1

        if odds >= 600:
            price_adjustment -= 10
        elif odds >= 300:
            price_adjustment -= 5

    value_adjustment = clamp(ev_pct * 0.35, -3, 4) + clamp(edge_pct * 0.45, -3, 4)
    validation = prob_pct + confidence_bonus + price_adjustment + value_adjustment
    return round(clamp(validation, 1, 95), 1)


def classify_three_colors(probability, validation, ev, edge, odds, market_type="ML"):
    probability = probability or 0
    validation = validation or 0
    ev = ev if ev is not None else 0
    edge = edge if edge is not None else 0

    # Cuotas extremadamente caras nunca deben ser verde.
    if odds is not None and odds <= -400:
        if validation >= 52 and probability >= 48:
            return {
                "color": "blue",
                "label": "PROBABLE",
                "action": "Probable",
                "stake": "0u-0.25u",
                "reason": "Probable ganador, pero cuota demasiado cara."
            }

    if validation >= 68 and probability >= 58 and ev > -3 and edge > -3:
        return {
            "color": "green",
            "label": "SEGURIDAD A JUGAR",
            "action": "Jugar",
            "stake": "0.50u-1u",
            "reason": "Alta validación con probabilidad sólida."
        }

    if validation >= 52 and probability >= 48:
        return {
            "color": "blue",
            "label": "PROBABLE",
            "action": "Probable",
            "stake": "0u-0.25u",
            "reason": "Probable ganador o mercado aceptable."
        }

    return {
        "color": "red",
        "label": "EVITAR",
        "action": "Evitar",
        "stake": "0u",
        "reason": "Baja validación o precio desfavorable."
    }


def confidence_score(validation):
    return round(clamp((validation or 0) / 10, 0.1, 9.9), 1)

def premium_tag(sig):
    odds = sig.get("odds")
    ev = sig.get("ev")
    validation = sig.get("validation", 0)
    market = sig.get("short_market", "")
    if market != "ML":
        return "🎯 Pick alternativo"
    if odds is not None and odds <= -300:
        return "⚠ Línea cara"
    if ev is not None and ev >= 3:
        return "💎 Value Pick"
    if validation >= 68:
        return "🔒 Favorito sólido"
    return "📌 Probable"

def sharp_warning(sig):
    odds = sig.get("odds")
    ev = sig.get("ev")
    validation = sig.get("validation", 0)
    if odds is not None and odds <= -400:
        return "Cuota muy cara; stake bajo."
    if ev is not None and ev < -5 and validation >= 55:
        return "Probable, pero sin valor fuerte."
    if validation < 52:
        return "No usar en tickets principales."
    return "Sin alerta fuerte."

def enrich_signal(sig):
    sig["confidence_score"] = confidence_score(sig.get("validation"))
    sig["premium_tag"] = premium_tag(sig)
    sig["sharp_warning"] = sharp_warning(sig)
    return sig


def moneyline_signals(game):
    outcomes = get_market_outcomes(game, "h2h")
    fair_probs, counts = no_vig_h2h_probabilities(outcomes)
    signals = []

    for name, fair_prob in fair_probs.items():
        best = best_price_filtered(outcomes, name, fair_prob)
        if not best:
            continue

        prob_pct = round(clamp(fair_prob * 100, 1, 85), 1)
        ev_pct, edge_pct = safe_ev_edge(fair_prob, best["american_odds"])
        book_count = counts.get(name, 0)

        validation = smooth_probability(
            prob_pct=prob_pct,
            odds=best["american_odds"],
            book_count=book_count,
            ev_pct=ev_pct,
            edge_pct=edge_pct
        )

        meta = classify_three_colors(
            probability=prob_pct,
            validation=validation,
            ev=ev_pct,
            edge=edge_pct,
            odds=best["american_odds"],
            market_type="ML"
        )

        sig = {
            "market": "Moneyline",
            "short_market": "ML",
            "selection": name,
            "probability": prob_pct,
            "validation": validation,
            "color": meta["color"],
            "label": meta["label"],
            "action": meta["action"],
            "stake": meta["stake"],
            "reason": meta["reason"],
            "ev": ev_pct,
            "edge": edge_pct,
            "odds": best["american_odds"],
            "point": None,
            "bookmaker": best["bookmaker"],
            "book_count": book_count,
            "is_primary": True,
        }
        signals.append(enrich_signal(sig))

    signals.sort(key=lambda x: (x["validation"], x["probability"]), reverse=True)
    return signals

def alt_market_signals(game):
    """
    Pick 2 usa spread/total. No repite ML.
    Para spread/total no inventamos EV real; usamos implied probability + ajuste de precio.
    """
    signals = []

    for market_key, short in [("spreads", "Spread"), ("totals", "Total")]:
        outcomes = get_market_outcomes(game, market_key)
        if not outcomes:
            continue

        seen = set()
        for out in outcomes:
            name = out.get("name")
            point = out.get("point")
            price = out.get("price")
            if name is None or price is None:
                continue

            key = (market_key, name, point)
            if key in seen:
                continue
            seen.add(key)

            best = best_price_same(outcomes, name, point)
            if not best:
                continue

            implied = implied_probability_american(best["american_odds"])
            if implied is None:
                continue

            prob_pct = round(clamp(implied * 100, 1, 85), 1)

            # Ajuste: precio razonable -110 / +100 suele ser mejor para spread/total.
            validation = prob_pct
            if -130 <= best["american_odds"] <= 120:
                validation += 6
            elif abs(best["american_odds"]) > 180:
                validation -= 8

            validation = round(clamp(validation, 1, 95), 1)

            if validation >= 56:
                color = "blue"
                label = "PROBABLE"
                action = "Probable"
                reason = "Mercado alternativo con precio razonable."
                stake = "0u-0.25u"
            else:
                color = "red"
                label = "EVITAR"
                action = "Evitar"
                reason = "No tiene suficiente validación como segunda jugada."
                stake = "0u"

            title = name
            if market_key == "totals":
                title = f"{name} {point}"
            elif market_key == "spreads":
                title = f"{name} {point:+g}"

            sig = {
                "market": short,
                "short_market": short,
                "selection": title,
                "probability": prob_pct,
                "validation": validation,
                "color": color,
                "label": label,
                "action": action,
                "stake": stake,
                "reason": reason,
                "ev": None,
                "edge": None,
                "odds": best["american_odds"],
                "point": point,
                "bookmaker": best["bookmaker"],
                "is_primary": False,
            }
            signals.append(enrich_signal(sig))

    signals.sort(key=lambda x: x["validation"], reverse=True)
    return signals

def game_prediction(game, sport_label):
    home = game.get("home_team")
    away = game.get("away_team")
    game_name = f"{away} vs {home}"

    ml = moneyline_signals(game)
    alt = alt_market_signals(game)

    primary = ml[0] if ml else None

    secondary = None
    for sig in alt:
        if sig["color"] != "red":
            secondary = sig
            break
    if secondary is None and alt:
        secondary = alt[0]

    all_signals = []
    if primary:
        all_signals.append(primary)
    if secondary:
        all_signals.append(secondary)

    return {
        "sport": sport_label,
        "game": game_name,
        "home_team": home,
        "away_team": away,
        "start_time": game.get("commence_time"),
        "primary_pick": primary,
        "secondary_pick": secondary,
        "signals": all_signals,
        "moneyline_options": ml,
        "alt_options": alt,
        "note": "v7: Top 5 + Smart Tickets 3/6/10."
    }

def dedupe_signals(items):
    seen = set()
    out = []
    for x in items:
        key = (x.get("game"), x.get("selection"), x.get("short_market"))
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out

def ticket_quality_pick(sig):
    if not sig or sig.get("color") == "red":
        return False
    if sig.get("validation", 0) < 52:
        return False
    odds = sig.get("odds")
    if odds is not None and (odds <= -450 or odds >= 700):
        return False
    return True

def build_smart_ticket(name, target_count, pool, color, risk, reason):
    picks, used_games, market_counts = [], set(), {}
    for sig in pool:
        if len(picks) >= target_count:
            break
        game = sig.get("game")
        market = sig.get("short_market", "")
        if game in used_games:
            continue
        if market_counts.get(market, 0) >= max(2, target_count // 2):
            continue
        picks.append(sig)
        used_games.add(game)
        market_counts[market] = market_counts.get(market, 0) + 1

    if len(picks) < target_count:
        existing = {(x.get("game"), x.get("selection"), x.get("short_market")) for x in picks}
        for sig in pool:
            if len(picks) >= target_count:
                break
            key = (sig.get("game"), sig.get("selection"), sig.get("short_market"))
            if key in existing:
                continue
            picks.append(sig)
            existing.add(key)

    if len(picks) < min(target_count, 2):
        return None

    combined = 1.0
    avg_val = 0
    for x in picks:
        combined *= clamp(x.get("probability", 1) / 100, 0.01, 0.99)
        avg_val += x.get("validation", 0)
    avg_val = avg_val / len(picks)
    return {
        "name": name,
        "color": color,
        "target_count": target_count,
        "picks": picks,
        "validation": round(clamp(avg_val, 1, 95), 1),
        "combined_probability": round(clamp(combined * 100, 0.1, 95), 1),
        "risk": risk,
        "reason": reason,
        "ticket_type": "Smart Aggressive"
    }

def build_smart_tickets(green, blue):
    pool = dedupe_signals(green + blue)
    pool = [x for x in pool if ticket_quality_pick(x)]
    pool.sort(key=lambda x: (x.get("validation") or 0, x.get("confidence_score") or 0), reverse=True)
    specs = [
        ("Ticket 3", 3, "green", "Medio/Alto", "Agresivo controlado con las mejores señales."),
        ("Ticket 6", 6, "blue", "Alto", "Ticket fuerte mezclando seguridad y probables."),
        ("Ticket 10", 10, "red", "Muy alto", "Ticket agresivo de alto retorno; stake pequeño."),
    ]
    tickets=[]
    for spec in specs:
        t=build_smart_ticket(*spec, pool=pool)
        if t:
            tickets.append(t)
    return tickets

def get_dashboard(selected_sports, force_refresh=False):
    dashboard = {
        "mode": "Pro v7 Smart Tickets",
        "credit_saving": True,
        "cache_ttl_seconds": CACHE_TTL,
        "only_today": ONLY_TODAY,
        "timezone": LOCAL_TZ,
        "sports": {},
        "games": [],
        "top5": [],
        "green": [],
        "blue": [],
        "red": [],
        "smart_tickets": [],
        "parlays": [],
        "warnings": []
    }

    for sport_label in selected_sports:
        sport_key = SPORTS.get(sport_label)
        if not sport_key:
            continue

        try:
            games, from_cache, ttl_left = fetch_odds(sport_key, force_refresh=force_refresh)
            total_games = len(games)
            games = [g for g in games if is_game_today(g)]

            dashboard["sports"][sport_label] = {
                "ok": True,
                "sport_key": sport_key,
                "games_count": len(games),
                "total_api_games": total_games,
                "today_only": ONLY_TODAY,
                "timezone": LOCAL_TZ,
                "from_cache": from_cache,
                "cache_seconds_left": ttl_left
            }

            for game in games:
                pred = game_prediction(game, sport_label)
                dashboard["games"].append(pred)

                if pred["primary_pick"]:
                    pick = pred["primary_pick"].copy()
                    pick.update({"sport": sport_label, "game": pred["game"], "start_time": pred["start_time"]})
                    if pick["color"] == "green":
                        dashboard["green"].append(pick)
                    elif pick["color"] == "blue":
                        dashboard["blue"].append(pick)
                    else:
                        dashboard["red"].append(pick)

                # También permite que el pick alternativo azul alimente secciones/parleys.
                if pred["secondary_pick"] and pred["secondary_pick"].get("color") in ("green", "blue"):
                    alt = pred["secondary_pick"].copy()
                    alt.update({"sport": sport_label, "game": pred["game"], "start_time": pred["start_time"]})
                    if alt["color"] == "green":
                        dashboard["green"].append(alt)
                    else:
                        dashboard["blue"].append(alt)

        except Exception as e:
            dashboard["sports"][sport_label] = {
                "ok": False,
                "sport_key": sport_key,
                "games_count": 0,
                "error": str(e)
            }
            dashboard["warnings"].append(f"{sport_label}: {str(e)}")

    sorter = lambda x: (x.get("validation") or 0, x.get("probability") or 0)
    dashboard["green"].sort(key=sorter, reverse=True)
    dashboard["blue"].sort(key=sorter, reverse=True)
    dashboard["red"].sort(key=sorter, reverse=True)

    dashboard["green"] = dedupe_signals(dashboard["green"])[:12]
    dashboard["blue"] = dedupe_signals(dashboard["blue"])[:18]
    dashboard["red"] = dedupe_signals(dashboard["red"])[:15]
    dashboard["top5"] = dedupe_signals(dashboard["green"] + dashboard["blue"])[:5]
    dashboard["smart_tickets"] = build_smart_tickets(dashboard["green"], dashboard["blue"])
    dashboard["parlays"] = dashboard["smart_tickets"]

    return dashboard

def debug_all_sports():
    result = {
        "api_key": api_key_status(),
        "regions": REGIONS,
        "markets": MARKETS,
        "odds_format": ODDS_FORMAT,
        "cache_ttl_seconds": CACHE_TTL,
        "only_today": ONLY_TODAY,
        "timezone": LOCAL_TZ,
        "sports": {}
    }

    for label, key in SPORTS.items():
        try:
            games, from_cache, ttl_left = fetch_odds(key)
            total_games = len(games)
            games = [g for g in games if is_game_today(g)]
            result["sports"][label] = {
                "ok": True,
                "sport_key": key,
                "games_count": len(games),
                "total_api_games": total_games,
                "today_only": ONLY_TODAY,
                "timezone": LOCAL_TZ,
                "from_cache": from_cache,
                "cache_seconds_left": ttl_left,
                "sample_games": [
                    {
                        "home_team": g.get("home_team"),
                        "away_team": g.get("away_team"),
                        "commence_time": g.get("commence_time"),
                        "bookmakers_count": len(g.get("bookmakers", []))
                    }
                    for g in games[:5]
                ]
            }
        except Exception as e:
            result["sports"][label] = {"ok": False, "sport_key": key, "games_count": 0, "error": str(e)}
    return result
