import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv('ODDS_API_KEY', '')
SPORTS = {'MLB': 'baseball_mlb', 'NBA': 'basketball_nba', 'NHL': 'icehockey_nhl'}
REGIONS = os.getenv('REGIONS', 'us')
MARKETS = os.getenv('MARKETS', 'h2h,spreads,totals')
ODDS_FORMAT = 'american'
LOCAL_TZ = os.getenv('LOCAL_TZ', 'America/New_York')
ONLY_TODAY = os.getenv('ONLY_TODAY', '1') == '1'
CACHE = {}
CACHE_TTL = int(os.getenv('CACHE_TTL', '900'))

def clear_cache():
    CACHE.clear()

def api_key_status():
    if not API_KEY:
        return 'missing'
    if len(API_KEY) < 10:
        return 'too_short'
    return 'present'

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
    if not API_KEY or API_KEY == 'pon_tu_api_key_aqui':
        raise RuntimeError('Falta ODDS_API_KEY en Render Environment')
    cache_key = f'{sport_key}:{REGIONS}:{MARKETS}:{ODDS_FORMAT}'
    now = time.time()
    if not force_refresh and cache_key in CACHE:
        cached_time, cached_data = CACHE[cache_key]
        if now - cached_time < CACHE_TTL:
            return cached_data, True, int(CACHE_TTL - (now - cached_time))
    url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/'
    params = {'apiKey': API_KEY, 'regions': REGIONS, 'markets': MARKETS, 'oddsFormat': ODDS_FORMAT, 'dateFormat': 'iso'}
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f'The Odds API error {r.status_code}: {detail}')
    data = r.json()
    CACHE[cache_key] = (now, data)
    return data, False, CACHE_TTL

def is_game_today(game):
    if not ONLY_TODAY:
        return True
    start = game.get('commence_time')
    if not start:
        return False
    try:
        dt_utc = datetime.fromisoformat(start.replace('Z', '+00:00'))
        local_zone = ZoneInfo(LOCAL_TZ)
        return dt_utc.astimezone(local_zone).date() == datetime.now(local_zone).date()
    except Exception:
        return False

def get_market_outcomes(game, market_key):
    rows = []
    for bookmaker in game.get('bookmakers', []):
        for market in bookmaker.get('markets', []):
            if market.get('key') != market_key:
                continue
            for outcome in market.get('outcomes', []):
                rows.append({'bookmaker': bookmaker.get('title', 'Unknown'), 'market': market_key, 'name': outcome.get('name'), 'price': outcome.get('price'), 'point': outcome.get('point')})
    return rows

def normalize_h2h_probs(outcomes):
    probs = []
    for out in outcomes:
        p = implied_probability_american(out.get('price'))
        if p is not None:
            probs.append((out, p))
    total = sum(p for _, p in probs)
    if total <= 0:
        return []
    return [(out, p / total) for out, p in probs]

def best_price_same(outcomes, name, point=None):
    best = None
    for out in outcomes:
        if out.get('name') != name:
            continue
        if point is not None and out.get('point') != point:
            continue
        price = out.get('price')
        if price is None:
            continue
        dec = american_to_decimal(price)
        if best is None or dec > best['decimal_odds']:
            best = {'bookmaker': out.get('bookmaker', 'Unknown'), 'american_odds': price, 'decimal_odds': dec, 'point': out.get('point')}
    return best

def validate_signal(prob, ev, edge, odds, market):
    prob = prob or 0
    ev = ev or 0
    edge = edge or 0
    odds = odds if odds is not None else 0
    validation = prob + max(min(edge, 6), -6) * 2.0 + max(min(ev, 10), -8) * 1.2
    if odds >= 300:
        validation -= 8
    if odds <= -250 and ev < -2:
        validation -= 6
    validation = max(1, min(99, round(validation, 1)))
    if (ev > 0 and edge > 0 and prob >= 45) or (prob >= 62 and ev > -2.5 and edge > -2):
        return {'validation': validation, 'color': 'green', 'label': 'SEGURIDAD A JUGAR', 'action': 'Jugar', 'stake': '0.50u-1u', 'reason': 'Buena combinación de probabilidad y precio.'}
    if prob >= 54 and ev > -4 and edge > -3:
        return {'validation': validation, 'color': 'blue', 'label': 'PROBABLE', 'action': 'Probable', 'stake': '0u-0.25u', 'reason': 'Tiene buena probabilidad, pero no es valor fuerte.'}
    return {'validation': validation, 'color': 'red', 'label': 'EVITAR', 'action': 'Evitar', 'stake': '0u', 'reason': 'No tiene suficiente ventaja o el precio está malo.'}

def moneyline_signals(game):
    outcomes = get_market_outcomes(game, 'h2h')
    norm = normalize_h2h_probs(outcomes)
    signals = []
    for out, fair_prob in norm:
        name = out.get('name')
        best = best_price_same(outcomes, name)
        if not best:
            continue
        implied = implied_probability_american(best['american_odds'])
        ev = calculate_ev(fair_prob, best['decimal_odds'])
        edge = fair_prob - implied
        prob_pct = round(fair_prob * 100, 1)
        ev_pct = round(ev * 100, 1)
        edge_pct = round(edge * 100, 1)
        meta = validate_signal(prob_pct, ev_pct, edge_pct, best['american_odds'], 'Moneyline')
        signals.append({'market': 'Moneyline', 'short_market': 'ML', 'selection': name, 'probability': prob_pct, 'validation': meta['validation'], 'color': meta['color'], 'label': meta['label'], 'action': meta['action'], 'stake': meta['stake'], 'reason': meta['reason'], 'ev': ev_pct, 'edge': edge_pct, 'odds': best['american_odds'], 'point': None, 'bookmaker': best['bookmaker'], 'is_primary': True})
    signals.sort(key=lambda x: (x['validation'], x['probability']), reverse=True)
    return signals

def alt_market_signals(game):
    signals = []
    for market_key, short in [('spreads', 'Spread'), ('totals', 'Total')]:
        outcomes = get_market_outcomes(game, market_key)
        if not outcomes:
            continue
        seen = set()
        for out in outcomes:
            name, point, price = out.get('name'), out.get('point'), out.get('price')
            if name is None or price is None:
                continue
            key = (market_key, name, point)
            if key in seen:
                continue
            seen.add(key)
            best = best_price_same(outcomes, name, point)
            if not best:
                continue
            implied = implied_probability_american(best['american_odds'])
            if implied is None:
                continue
            prob_pct = round(implied * 100, 1)
            validation = prob_pct + (4 if -130 <= best['american_odds'] <= 120 else 0) - (8 if abs(best['american_odds']) > 180 else 0)
            validation = max(1, min(99, round(validation, 1)))
            color = 'blue' if validation >= 55 else 'red'
            label = 'PROBABLE' if color == 'blue' else 'EVITAR'
            action = 'Probable' if color == 'blue' else 'Evitar'
            reason = 'Mercado alternativo con precio razonable.' if color == 'blue' else 'No tiene suficiente validación como segunda jugada.'
            stake = '0u-0.25u' if color == 'blue' else '0u'
            title = f'{name} {point}' if market_key == 'totals' else f'{name} {point:+g}'
            signals.append({'market': short, 'short_market': short, 'selection': title, 'probability': prob_pct, 'validation': validation, 'color': color, 'label': label, 'action': action, 'stake': stake, 'reason': reason, 'ev': None, 'edge': None, 'odds': best['american_odds'], 'point': point, 'bookmaker': best['bookmaker'], 'is_primary': False})
    signals.sort(key=lambda x: x['validation'], reverse=True)
    return signals

def game_prediction(game, sport_label):
    home = game.get('home_team')
    away = game.get('away_team')
    game_name = f'{away} vs {home}'
    ml = moneyline_signals(game)
    alt = alt_market_signals(game)
    primary = ml[0] if ml else None
    secondary = next((sig for sig in alt if sig['color'] != 'red'), alt[0] if alt else None)
    return {'sport': sport_label, 'game': game_name, 'home_team': home, 'away_team': away, 'start_time': game.get('commence_time'), 'primary_pick': primary, 'secondary_pick': secondary, 'signals': [s for s in [primary, secondary] if s], 'moneyline_options': ml, 'alt_options': alt, 'note': 'Pick 1 ML. Pick 2 usa Spread o Total para no repetir ML.'}

def build_parlays(top_green, probable_blue):
    parlays = []
    def parlay_prob(picks):
        p = 1.0
        for x in picks:
            p *= max(0.01, min(0.99, x.get('probability', 1) / 100))
        return round(p * 100, 1)
    if len(top_green) >= 2:
        picks = top_green[:2]
        parlays.append({'name': 'Parley Seguro', 'color': 'green', 'picks': picks, 'validation': parlay_prob(picks), 'risk': 'Medio', 'reason': 'Usa dos señales verdes.'})
    if len(top_green) >= 1 and len(probable_blue) >= 2:
        picks = [top_green[0]] + probable_blue[:2]
        parlays.append({'name': 'Parley Balanceado', 'color': 'blue', 'picks': picks, 'validation': parlay_prob(picks), 'risk': 'Medio/Alto', 'reason': 'Mezcla una verde con dos probables.'})
    elif len(probable_blue) >= 2:
        picks = probable_blue[:2]
        parlays.append({'name': 'Parley Probable', 'color': 'blue', 'picks': picks, 'validation': parlay_prob(picks), 'risk': 'Medio/Alto', 'reason': 'No hay verdes; usa las mejores probables.'})
    if len(probable_blue) >= 3:
        picks = probable_blue[:3]
        parlays.append({'name': 'Parley Agresivo', 'color': 'red', 'picks': picks, 'validation': parlay_prob(picks), 'risk': 'Alto', 'reason': 'Tres probables. Más pago, más riesgo.'})
    return parlays

def get_dashboard(selected_sports, force_refresh=False):
    dashboard = {'mode': 'Pro v5 Clean', 'credit_saving': True, 'cache_ttl_seconds': CACHE_TTL, 'only_today': ONLY_TODAY, 'timezone': LOCAL_TZ, 'sports': {}, 'games': [], 'green': [], 'blue': [], 'red': [], 'parlays': [], 'warnings': []}
    for sport_label in selected_sports:
        sport_key = SPORTS.get(sport_label)
        if not sport_key:
            continue
        try:
            games, from_cache, ttl_left = fetch_odds(sport_key, force_refresh=force_refresh)
            total_games = len(games)
            games = [g for g in games if is_game_today(g)]
            dashboard['sports'][sport_label] = {'ok': True, 'sport_key': sport_key, 'games_count': len(games), 'total_api_games': total_games, 'today_only': ONLY_TODAY, 'timezone': LOCAL_TZ, 'from_cache': from_cache, 'cache_seconds_left': ttl_left}
            for game in games:
                pred = game_prediction(game, sport_label)
                dashboard['games'].append(pred)
                if pred['primary_pick']:
                    pick = pred['primary_pick'].copy()
                    pick.update({'sport': sport_label, 'game': pred['game'], 'start_time': pred['start_time']})
                    dashboard[pick['color']].append(pick)
        except Exception as e:
            dashboard['sports'][sport_label] = {'ok': False, 'sport_key': sport_key, 'games_count': 0, 'error': str(e)}
            dashboard['warnings'].append(f'{sport_label}: {str(e)}')
    sorter = lambda x: (x.get('validation') or 0, x.get('probability') or 0)
    for key in ['green', 'blue', 'red']:
        dashboard[key].sort(key=sorter, reverse=True)
    dashboard['green'] = dashboard['green'][:10]
    dashboard['blue'] = dashboard['blue'][:12]
    dashboard['red'] = dashboard['red'][:15]
    dashboard['parlays'] = build_parlays(dashboard['green'], dashboard['blue'])
    return dashboard

def debug_all_sports():
    result = {'api_key': api_key_status(), 'regions': REGIONS, 'markets': MARKETS, 'odds_format': ODDS_FORMAT, 'cache_ttl_seconds': CACHE_TTL, 'only_today': ONLY_TODAY, 'timezone': LOCAL_TZ, 'sports': {}}
    for label, key in SPORTS.items():
        try:
            games, from_cache, ttl_left = fetch_odds(key)
            total_games = len(games)
            games = [g for g in games if is_game_today(g)]
            result['sports'][label] = {'ok': True, 'sport_key': key, 'games_count': len(games), 'total_api_games': total_games, 'today_only': ONLY_TODAY, 'timezone': LOCAL_TZ, 'from_cache': from_cache, 'cache_seconds_left': ttl_left, 'sample_games': [{'home_team': g.get('home_team'), 'away_team': g.get('away_team'), 'commence_time': g.get('commence_time'), 'bookmakers_count': len(g.get('bookmakers', []))} for g in games[:5]]}
        except Exception as e:
            result['sports'][label] = {'ok': False, 'sport_key': key, 'games_count': 0, 'error': str(e)}
    return result
