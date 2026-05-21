import os, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()
API_KEY=os.getenv('ODDS_API_KEY','')
SPORTS={'MLB':'baseball_mlb','NBA':'basketball_nba','NHL':'icehockey_nhl'}
REGIONS=os.getenv('REGIONS','us'); MARKETS=os.getenv('MARKETS','h2h,spreads,totals'); ODDS_FORMAT='american'
LOCAL_TZ=os.getenv('LOCAL_TZ','America/New_York'); ONLY_TODAY=os.getenv('ONLY_TODAY','1')=='1'
CACHE={}; CACHE_TTL=int(os.getenv('CACHE_TTL','900'))
def clamp(v,a,b):
    try: return max(a,min(b,float(v)))
    except Exception: return a
def clear_cache(): CACHE.clear()
def api_key_status():
    if not API_KEY: return 'missing'
    if len(API_KEY)<10: return 'too_short'
    return 'present'
def american_to_decimal(o):
    if o is None: return None
    o=float(o); return 1+o/100 if o>0 else 1+100/abs(o)
def implied_probability_american(o):
    if o is None: return None
    o=float(o); return 100/(o+100) if o>0 else abs(o)/(abs(o)+100)
def calculate_ev(p,d):
    if p is None or d is None: return None
    return p*d-1
def fetch_odds(sport_key, force_refresh=False):
    if not API_KEY or API_KEY=='pon_tu_api_key_aqui': raise RuntimeError('Falta ODDS_API_KEY en Render Environment')
    key=f'{sport_key}:{REGIONS}:{MARKETS}:{ODDS_FORMAT}'; now=time.time()
    if not force_refresh and key in CACHE:
        ts,data=CACHE[key]
        if now-ts<CACHE_TTL: return data, True, int(CACHE_TTL-(now-ts))
    url=f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds/'
    params={'apiKey':API_KEY,'regions':REGIONS,'markets':MARKETS,'oddsFormat':ODDS_FORMAT,'dateFormat':'iso'}
    r=requests.get(url,params=params,timeout=30)
    if r.status_code!=200:
        try: detail=r.json()
        except Exception: detail=r.text
        raise RuntimeError(f'The Odds API error {r.status_code}: {detail}')
    data=r.json(); CACHE[key]=(now,data); return data, False, CACHE_TTL
def is_game_today(game):
    if not ONLY_TODAY: return True
    s=game.get('commence_time')
    if not s: return False
    try:
        dt=datetime.fromisoformat(s.replace('Z','+00:00')); z=ZoneInfo(LOCAL_TZ)
        return dt.astimezone(z).date()==datetime.now(z).date()
    except Exception: return False
def get_market_outcomes(game,mkey):
    rows=[]
    for b in game.get('bookmakers',[]):
        title=b.get('title','Unknown')
        for m in b.get('markets',[]):
            if m.get('key')!=mkey: continue
            for o in m.get('outcomes',[]): rows.append({'bookmaker':title,'market':mkey,'name':o.get('name'),'price':o.get('price'),'point':o.get('point')})
    return rows
def best_price_same(outcomes,name,point=None):
    best=None
    for o in outcomes:
        if o.get('name')!=name: continue
        if point is not None and o.get('point')!=point: continue
        price=o.get('price'); dec=american_to_decimal(price)
        if price is None or dec is None: continue
        if best is None or dec>best['decimal_odds']: best={'bookmaker':o.get('bookmaker','Unknown'),'american_odds':int(price),'decimal_odds':dec,'point':o.get('point')}
    return best
def no_vig_h2h_probabilities(outcomes):
    by={}
    for o in outcomes:
        n=o.get('name'); price=o.get('price'); book=o.get('bookmaker','Unknown')
        if n is None or price is None: continue
        p=implied_probability_american(price)
        if p is not None: by.setdefault(book,[]).append((n,p))
    probs={}; counts={}
    for items in by.values():
        if len(items)<2: continue
        total=sum(p for _,p in items)
        if total<=0: continue
        for n,p in items:
            fair=p/total; probs[n]=probs.get(n,0)+fair; counts[n]=counts.get(n,0)+1
    return {n:clamp(v/counts.get(n,1),.01,.99) for n,v in probs.items()}, counts
def best_price_filtered(outcomes,name,fair,point=None):
    cand=[]
    for o in outcomes:
        if o.get('name')!=name: continue
        if point is not None and o.get('point')!=point: continue
        price=o.get('price'); imp=implied_probability_american(price); dec=american_to_decimal(price)
        if price is None or imp is None or dec is None: continue
        cand.append({'bookmaker':o.get('bookmaker','Unknown'),'american_odds':int(price),'decimal_odds':dec,'point':o.get('point'),'diff':abs(imp-fair)})
    if not cand: return None
    sane=[c for c in cand if c['diff']<=.25]
    if not sane: return sorted(cand,key=lambda c:c['diff'])[0]
    return sorted(sane,key=lambda c:c['decimal_odds'], reverse=True)[0]
def safe_ev_edge(fair,odds):
    dec=american_to_decimal(odds); imp=implied_probability_american(odds)
    if dec is None or imp is None: return None,None
    return round(clamp(calculate_ev(fair,dec)*100,-25,25),1), round(clamp((fair-imp)*100,-20,20),1)
def smooth_validation(prob,odds,books,ev=0,edge=0):
    prob=clamp(prob,1,85); ev=clamp(ev or 0,-10,10); edge=clamp(edge or 0,-8,8); adj=min((books or 0)*.6,5)
    if odds is not None:
        if odds<=-1000: adj-=18
        elif odds<=-400: adj-=10
        elif odds<=-250: adj-=5
        elif odds<=-150: adj+=1
        if odds>=600: adj-=10
        elif odds>=300: adj-=5
    adj+=clamp(ev*.35,-3,4)+clamp(edge*.45,-3,4)
    return round(clamp(prob+adj,1,95),1)
def classify_pick(prob,val,ev,edge,odds):
    ev=ev if ev is not None else 0; edge=edge if edge is not None else 0
    if odds is not None and odds<=-400 and val>=52 and prob>=48: return 'blue','PROBABLE','Probable ganador, pero cuota demasiado cara.','0u-0.25u'
    if val>=68 and prob>=58 and ev>-3 and edge>-3: return 'green','SEGURIDAD A JUGAR','Alta validación con probabilidad sólida.','0.50u-1u'
    if val>=52 and prob>=48: return 'blue','PROBABLE','Probable ganador o mercado aceptable.','0u-0.25u'
    return 'red','EVITAR','Baja validación o precio desfavorable.','0u'
def confidence_score(v): return round(clamp((v or 0)/10,.1,9.9),1)
def premium_tag(sig):
    if sig.get('short_market')!='ML': return '🎯 Pick alternativo'
    if sig.get('odds') is not None and sig.get('odds')<=-300: return '⚠ Línea cara'
    if sig.get('ev') is not None and sig.get('ev')>=3: return '💎 Value Pick'
    if sig.get('validation',0)>=68: return '🔒 Favorito sólido'
    return '📌 Probable'
def sharp_warning(sig):
    if sig.get('odds') is not None and sig.get('odds')<=-400: return 'Cuota muy cara; usar stake bajo.'
    if sig.get('ev') is not None and sig.get('ev')<-5 and sig.get('validation',0)>=55: return 'Probable, pero sin valor fuerte.'
    if sig.get('validation',0)<52: return 'No usar en tickets principales.'
    return 'Sin alerta fuerte.'
def enrich(sig):
    sig['confidence_score']=confidence_score(sig.get('validation')); sig['premium_tag']=premium_tag(sig); sig['sharp_warning']=sharp_warning(sig); sig['is_bet_recommendation']=sig.get('color') in ('green','blue'); return sig
def moneyline_signals(game):
    outs=get_market_outcomes(game,'h2h'); fair,counts=no_vig_h2h_probabilities(outs); signals=[]
    for name,p in fair.items():
        best=best_price_filtered(outs,name,p)
        if not best: continue
        odds=best['american_odds']; prob=round(clamp(p*100,1,85),1); ev,edge=safe_ev_edge(p,odds); val=smooth_validation(prob,odds,counts.get(name,0),ev,edge); color,label,reason,stake=classify_pick(prob,val,ev,edge,odds)
        signals.append(enrich({'market':'Moneyline','short_market':'ML','selection':name,'probability':prob,'validation':val,'color':color,'label':label,'reason':reason,'stake':stake,'ev':ev,'edge':edge,'odds':odds,'point':None,'bookmaker':best['bookmaker'],'book_count':counts.get(name,0),'is_primary':True}))
    return sorted(signals,key=lambda x:(x['validation'],x['probability']), reverse=True)
def alt_market_signals(game):
    signals=[]
    for key,short in [('spreads','Spread'),('totals','Total')]:
        outs=get_market_outcomes(game,key); seen=set()
        for o in outs:
            name=o.get('name'); point=o.get('point'); price=o.get('price')
            if name is None or price is None: continue
            k=(key,name,point)
            if k in seen: continue
            seen.add(k); best=best_price_same(outs,name,point)
            if not best: continue
            imp=implied_probability_american(best['american_odds'])
            if imp is None: continue
            prob=round(clamp(imp*100,1,85),1); val=prob
            if -130<=best['american_odds']<=120: val+=6
            elif abs(best['american_odds'])>180: val-=8
            val=round(clamp(val,1,95),1)
            color,label,reason,stake=('blue','PROBABLE','Mercado alternativo con precio razonable.','0u-0.25u') if val>=58 else ('red','EVITAR','No tiene suficiente validación como segunda jugada.','0u')
            title=f'{name} {point}' if key=='totals' else f'{name} {point:+g}'
            signals.append(enrich({'market':short,'short_market':short,'selection':title,'probability':prob,'validation':val,'color':color,'label':label,'reason':reason,'stake':stake,'ev':None,'edge':None,'odds':best['american_odds'],'point':point,'bookmaker':best['bookmaker'],'is_primary':False}))
    return sorted(signals,key=lambda x:x['validation'], reverse=True)
def prediction_summary(ml):
    if not ml: return None
    w=sorted(ml,key=lambda x:x['probability'], reverse=True)[0]
    return {'selection':w['selection'],'probability':w['probability'],'odds':w['odds'],'bookmaker':w['bookmaker'],'note':'Pronóstico ML separado: indica quién tiene más probabilidad de ganar, no necesariamente que conviene apostarlo.'}
def best_bet_for_game(primary,secondary):
    candidates=[x for x in [secondary,primary] if x]
    playable=[x for x in candidates if x.get('color') in ('green','blue')]
    if playable: return sorted(playable,key=lambda x:(x.get('validation',0),x.get('probability',0)), reverse=True)[0]
    return secondary or primary
def game_prediction(game,sport):
    name=f"{game.get('away_team')} vs {game.get('home_team')}"; ml=moneyline_signals(game); alt=alt_market_signals(game); primary=ml[0] if ml else None; secondary=next((s for s in alt if s['color']!='red'), alt[0] if alt else None); best=best_bet_for_game(primary,secondary)
    return {'sport':sport,'game':name,'home_team':game.get('home_team'),'away_team':game.get('away_team'),'start_time':game.get('commence_time'),'ml_prediction':prediction_summary(ml),'primary_pick':primary,'secondary_pick':secondary,'best_bet':best,'signals':[x for x in [primary,secondary] if x],'moneyline_options':ml,'alt_options':alt,'note':'v8.1: pronóstico ganador separado de recomendación de apuesta.'}
def dedupe(items):
    seen=set(); out=[]
    for x in items:
        k=(x.get('game'),x.get('selection'),x.get('short_market'))
        if k not in seen: seen.add(k); out.append(x)
    return out
def ticket_ok(sig):
    if not sig or sig.get('color')=='red' or sig.get('validation',0)<58: return False
    odds=sig.get('odds')
    if odds is not None and (odds<=-450 or odds>=700): return False
    return True
def build_ticket(name,count,pool,color,risk,reason):
    picks=[]; games=set()
    for s in pool:
        if len(picks)>=count: break
        if s.get('game') in games or not ticket_ok(s): continue
        picks.append(s); games.add(s.get('game'))
    if name!='Ticket 10' and len(picks)<count: return None
    if name=='Ticket 10' and len(picks)<6: return None
    comb=1; avg=0
    for x in picks: comb*=clamp(x.get('probability',1)/100,.01,.99); avg+=x.get('validation',0)
    avg=avg/len(picks)
    return {'name':name,'color':color,'target_count':count,'picks':picks,'validation':round(clamp(avg,1,95),1),'combined_probability':round(clamp(comb*100,.1,95),1),'risk':risk,'reason':reason,'ticket_type':'Profit Ticket' if name!='Ticket 10' else 'Lottery Ticket'}
def build_tickets(green,blue):
    pool=dedupe(green+blue); pool.sort(key=lambda x:(x.get('validation',0),x.get('confidence_score',0)), reverse=True)
    tickets=[build_ticket('Ticket 3',3,pool,'green','Medio','Ticket principal: solo picks con mejor validación.'),build_ticket('Ticket 6',6,pool,'blue','Alto','Solo se genera si hay 6 señales fuertes.'),build_ticket('Ticket 10',10,pool,'red','Extremo','Lottery Ticket: alto retorno, usar stake mínimo.')]
    return [t for t in tickets if t]
def get_dashboard(selected_sports, force_refresh=False):
    dash={'mode':'Pro v8.1 Clear Logic','credit_saving':True,'cache_ttl_seconds':CACHE_TTL,'only_today':ONLY_TODAY,'timezone':LOCAL_TZ,'sports':{},'games':[],'top_profit':[],'green':[],'blue':[],'red':[],'tickets':[],'warnings':[]}
    for label in selected_sports:
        key=SPORTS.get(label)
        if not key: continue
        try:
            games,from_cache,ttl=fetch_odds(key,force_refresh=force_refresh); total=len(games); games=[g for g in games if is_game_today(g)]
            dash['sports'][label]={'ok':True,'sport_key':key,'games_count':len(games),'total_api_games':total,'today_only':ONLY_TODAY,'timezone':LOCAL_TZ,'from_cache':from_cache,'cache_seconds_left':ttl}
            for g in games:
                pred=game_prediction(g,label); dash['games'].append(pred); bb=pred.get('best_bet')
                if bb and bb.get('color') in ('green','blue'):
                    p=bb.copy(); p.update({'sport':label,'game':pred['game'],'start_time':pred['start_time']}); dash[p['color']].append(p)
                elif pred.get('primary_pick'):
                    p=pred['primary_pick'].copy(); p.update({'sport':label,'game':pred['game'],'start_time':pred['start_time']}); dash['red'].append(p)
        except Exception as e:
            dash['sports'][label]={'ok':False,'sport_key':key,'games_count':0,'error':str(e)}; dash['warnings'].append(f'{label}: {e}')
    sorter=lambda x:(x.get('validation',0),x.get('confidence_score',0),x.get('probability',0))
    dash['green']=dedupe(sorted(dash['green'],key=sorter,reverse=True))[:10]; dash['blue']=dedupe(sorted(dash['blue'],key=sorter,reverse=True))[:14]; dash['red']=dedupe(sorted(dash['red'],key=sorter,reverse=True))[:15]
    dash['top_profit']=dedupe(dash['green']+dash['blue'])[:3]; dash['tickets']=build_tickets(dash['green'],dash['blue']); return dash
def debug_all_sports():
    res={'api_key':api_key_status(),'regions':REGIONS,'markets':MARKETS,'odds_format':ODDS_FORMAT,'cache_ttl_seconds':CACHE_TTL,'only_today':ONLY_TODAY,'timezone':LOCAL_TZ,'sports':{}}
    for label,key in SPORTS.items():
        try:
            games,fc,ttl=fetch_odds(key); total=len(games); games=[g for g in games if is_game_today(g)]
            res['sports'][label]={'ok':True,'sport_key':key,'games_count':len(games),'total_api_games':total,'today_only':ONLY_TODAY,'timezone':LOCAL_TZ,'from_cache':fc,'cache_seconds_left':ttl,'sample_games':[{'home_team':g.get('home_team'),'away_team':g.get('away_team'),'commence_time':g.get('commence_time'),'bookmakers_count':len(g.get('bookmakers',[]))} for g in games[:5]]}
        except Exception as e: res['sports'][label]={'ok':False,'sport_key':key,'games_count':0,'error':str(e)}
    return res
