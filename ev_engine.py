
import os, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()
API_KEY=os.getenv('ODDS_API_KEY','')
SPORTS={'MLB':'baseball_mlb','NBA':'basketball_nba','NHL':'icehockey_nhl'}
REGIONS=os.getenv('REGIONS','us')
MARKETS=os.getenv('MARKETS','h2h,spreads,totals')
ODDS_FORMAT='american'
LOCAL_TZ=os.getenv('LOCAL_TZ','America/New_York')
ONLY_TODAY=os.getenv('ONLY_TODAY','1')=='1'
CACHE={}; CACHE_TTL=int(os.getenv('CACHE_TTL','900'))

def clamp(v,a,b):
    try: return max(a,min(b,float(v)))
    except Exception: return a

def clear_cache(): CACHE.clear()
def api_key_status(): return 'missing' if not API_KEY else ('too_short' if len(API_KEY)<10 else 'present')
def american_to_decimal(o):
    if o is None: return None
    o=float(o); return 1+o/100 if o>0 else 1+100/abs(o)
def implied_probability_american(o):
    if o is None: return None
    o=float(o); return 100/(o+100) if o>0 else abs(o)/(abs(o)+100)
def calculate_ev(p,dec): return None if p is None or dec is None else p*dec-1

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

def is_game_today(g):
    if not ONLY_TODAY: return True
    s=g.get('commence_time')
    if not s: return False
    try:
        dt=datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(ZoneInfo(LOCAL_TZ))
        return dt.date()==datetime.now(ZoneInfo(LOCAL_TZ)).date()
    except Exception: return False

def get_market_outcomes(g, market_key):
    rows=[]
    for b in g.get('bookmakers',[]):
        book=b.get('title','Unknown')
        for m in b.get('markets',[]):
            if m.get('key')!=market_key: continue
            for o in m.get('outcomes',[]): rows.append({'bookmaker':book,'market':market_key,'name':o.get('name'),'price':o.get('price'),'point':o.get('point')})
    return rows

def best_price_same(outs,name,point=None):
    best=None
    for o in outs:
        if o.get('name')!=name: continue
        if point is not None and o.get('point')!=point: continue
        price=o.get('price'); dec=american_to_decimal(price)
        if price is None or dec is None: continue
        if best is None or dec>best['decimal_odds']:
            best={'bookmaker':o.get('bookmaker','Unknown'),'american_odds':int(price),'decimal_odds':dec,'point':o.get('point')}
    return best

def no_vig_h2h_probabilities(outs):
    by={}
    for o in outs:
        if o.get('name') is None or o.get('price') is None: continue
        p=implied_probability_american(o.get('price'))
        if p is None: continue
        by.setdefault(o.get('bookmaker','Unknown'),[]).append((o.get('name'),p))
    probs={}; counts={}
    for book,items in by.items():
        if len(items)<2: continue
        total=sum(p for _,p in items)
        if total<=0: continue
        for name,p in items:
            fair=p/total; probs[name]=probs.get(name,0)+fair; counts[name]=counts.get(name,0)+1
    return {n:clamp(v/counts.get(n,1),.01,.99) for n,v in probs.items()}, counts

def best_price_filtered(outs,name,fair,point=None):
    c=[]
    for o in outs:
        if o.get('name')!=name: continue
        if point is not None and o.get('point')!=point: continue
        price=o.get('price'); imp=implied_probability_american(price); dec=american_to_decimal(price)
        if price is None or imp is None or dec is None: continue
        c.append({'bookmaker':o.get('bookmaker','Unknown'),'american_odds':int(price),'decimal_odds':dec,'point':o.get('point'),'diff':abs(imp-fair)})
    if not c: return None
    sane=[x for x in c if x['diff']<=.25]
    return sorted(sane or c, key=lambda x: (x['diff'] if not sane else -x['decimal_odds']))[0] if not sane else sorted(sane,key=lambda x:x['decimal_odds'], reverse=True)[0]

def safe_ev_edge(fair,odds):
    dec=american_to_decimal(odds); imp=implied_probability_american(odds)
    if dec is None or imp is None: return None,None
    ev=calculate_ev(fair,dec); edge=fair-imp
    return round(clamp(ev*100,-25,25),1), round(clamp(edge*100,-20,20),1)

def smooth_validation(prob,odds,book_count,ev=0,edge=0):
    prob=clamp(prob,1,85); ev=clamp(ev or 0,-10,10); edge=clamp(edge or 0,-8,8)
    bonus=min((book_count or 0)*.55,4.5); adj=0
    if odds is not None:
        if odds<=-450: adj-=8
        elif odds<=-250: adj-=4
        elif odds<=-150: adj+=.5
        if odds>=450: adj-=8
        elif odds>=250: adj-=4
    value=clamp(ev*.25,-2.5,3)+clamp(edge*.35,-2.5,3)
    return round(clamp(prob+bonus+adj+value,1,92),1)

def classify(prob,val,ev,edge,odds):
    if odds is not None and (odds<=-450 or odds>=450):
        return {'color':'red','label':'EVITAR','stake':'0u','reason':'Línea demasiado riesgosa para modo rentable.'}
    if val>=70 and prob>=60 and (ev or 0)>-2.5 and (edge or 0)>-2.5:
        return {'color':'green','label':'ELITE','stake':'0.5u','reason':'Alta probabilidad y riesgo controlado.'}
    if val>=62 and prob>=56:
        return {'color':'green','label':'FUERTE','stake':'0.25u-0.5u','reason':'Pick fuerte para modo rentable.'}
    if val>=54 and prob>=50:
        return {'color':'blue','label':'PROBABLE','stake':'0u-0.25u','reason':'Jugable solo pequeño o para ticket.'}
    return {'color':'red','label':'EVITAR','stake':'0u','reason':'No pasa filtros de rentabilidad.'}

def confidence_score(v): return round(clamp(v/10,0.1,9.2),1)
def premium_tag(s):
    if s.get('short_market')!='ML': return '🎯 Pick alternativo'
    if s.get('label')=='ELITE': return '🔥 Elite'
    if s.get('label')=='FUERTE': return '🔒 Fuerte'
    if s.get('odds') is not None and s.get('odds')<=-250: return '⚠ Línea cara'
    if s.get('ev') is not None and s.get('ev')>=2: return '💎 Value'
    return '📌 Probable'
def sharp_warning(s):
    if s.get('color')=='red': return 'No usar en ticket rentable.'
    if s.get('ev') is not None and s.get('ev')<-4: return 'Probable, pero poco valor.'
    if s.get('odds') is not None and abs(s.get('odds'))>250: return 'Cuota sensible; stake bajo.'
    return 'Sin alerta fuerte.'
def enrich(s): s['confidence_score']=confidence_score(s.get('validation',0)); s['premium_tag']=premium_tag(s); s['sharp_warning']=sharp_warning(s); return s

def moneyline_signals(g):
    outs=get_market_outcomes(g,'h2h'); fair,counts=no_vig_h2h_probabilities(outs); sigs=[]
    for name,p in fair.items():
        best=best_price_filtered(outs,name,p)
        if not best: continue
        odds=best['american_odds']; prob=round(clamp(p*100,1,85),1); ev,edge=safe_ev_edge(p,odds)
        val=smooth_validation(prob,odds,counts.get(name,0),ev,edge); meta=classify(prob,val,ev,edge,odds)
        sig={'market':'Moneyline','short_market':'ML','selection':name,'probability':prob,'validation':val,'color':meta['color'],'label':meta['label'],'action':meta['label'],'stake':meta['stake'],'reason':meta['reason'],'ev':ev,'edge':edge,'odds':odds,'point':None,'bookmaker':best['bookmaker'],'book_count':counts.get(name,0),'is_primary':True}
        sigs.append(enrich(sig))
    return sorted(sigs,key=lambda x:(x['validation'],x['probability']),reverse=True)

def alt_market_signals(g):
    sigs=[]
    for mk,short in [('spreads','Spread'),('totals','Total')]:
        outs=get_market_outcomes(g,mk)
        seen=set()
        for o in outs:
            name=o.get('name'); point=o.get('point'); price=o.get('price')
            if name is None or price is None: continue
            key=(name,point); 
            if key in seen: continue
            seen.add(key); best=best_price_same(outs,name,point)
            if not best: continue
            imp=implied_probability_american(best['american_odds'])
            if imp is None: continue
            prob=round(clamp(imp*100,1,85),1); val=prob
            if -130<=best['american_odds']<=120: val+=5
            elif abs(best['american_odds'])>170: val-=7
            val=round(clamp(val,1,90),1)
            if val>=60: color,label,stake,reason='green','FUERTE','0.25u','Alternativa fuerte con precio razonable.'
            elif val>=55: color,label,stake,reason='blue','PROBABLE','0u-0.25u','Mercado alternativo aceptable.'
            else: color,label,stake,reason='red','EVITAR','0u','No pasa filtros alternativos.'
            title=f'{name} {point}' if mk=='totals' else f'{name} {point:+g}'
            sig={'market':short,'short_market':short,'selection':title,'probability':prob,'validation':val,'color':color,'label':label,'action':label,'stake':stake,'reason':reason,'ev':None,'edge':None,'odds':best['american_odds'],'point':point,'bookmaker':best['bookmaker'],'is_primary':False}
            sigs.append(enrich(sig))
    return sorted(sigs,key=lambda x:x['validation'],reverse=True)

def game_prediction(g,sport):
    home=g.get('home_team'); away=g.get('away_team'); game=f'{away} vs {home}'
    ml=moneyline_signals(g); alt=alt_market_signals(g)
    primary=ml[0] if ml else None
    secondary=next((x for x in alt if x.get('color')!='red'), alt[0] if alt else None)
    return {'sport':sport,'game':game,'home_team':home,'away_team':away,'start_time':g.get('commence_time'),'primary_pick':primary,'secondary_pick':secondary,'signals':[x for x in [primary,secondary] if x],'moneyline_options':ml,'alt_options':alt,'note':'v8 Profit Mode: menos picks, más filtro, ticket 10 solo lottery.'}

def dedupe(items):
    seen=set(); out=[]
    for x in items:
        k=(x.get('game'),x.get('selection'),x.get('short_market'))
        if k not in seen: seen.add(k); out.append(x)
    return out

def ticket_quality(s, tier='main'):
    if not s or s.get('color')=='red': return False
    if s.get('validation',0)<(62 if tier=='main' else 56): return False
    odds=s.get('odds')
    if odds is not None and (odds<=-450 or odds>=450): return False
    return True

def pool(green,blue,tier='main'):
    arr=dedupe(green+blue); arr=[x for x in arr if ticket_quality(x,tier)]
    return sorted(arr,key=lambda x:(x.get('validation',0),x.get('confidence_score',0)),reverse=True)

def make_ticket(name,n,pool_items,color,risk,reason,minimum=None):
    picks=[]; used_games=set(); markets={}; minimum = minimum or min(n,2)
    for s in pool_items:
        if len(picks)>=n: break
        if s.get('game') in used_games: continue
        m=s.get('short_market','')
        if markets.get(m,0)>=max(2,n//2): continue
        picks.append(s); used_games.add(s.get('game')); markets[m]=markets.get(m,0)+1
    if len(picks)<minimum:
        return {'name':name,'color':'red','target_count':n,'picks':[],'validation':0,'combined_probability':0,'risk':risk,'reason':'No hay suficientes picks premium hoy. Mejor no forzar ticket.','ticket_type':'No recomendado'}
    cp=1; avg=0
    for p in picks: cp*=clamp(p.get('probability',1)/100,.01,.99); avg+=p.get('validation',0)
    avg=avg/len(picks)
    return {'name':name,'color':color,'target_count':n,'picks':picks,'validation':round(avg,1),'combined_probability':round(clamp(cp*100,.1,95),1),'risk':risk,'reason':reason,'ticket_type':'Profit Ticket' if n<10 else 'Lottery Ticket'}

def build_tickets(green,blue):
    main=pool(green,blue,'main'); lotto=pool(green,blue,'lotto')
    return [
        make_ticket('Ticket 3 Rentable',3,main,'green','Medio','Ticket principal. Menos picks, más probabilidad.',minimum=3),
        make_ticket('Ticket 6 Selectivo',6,main,'blue','Alto','Solo si existen 6 picks fuertes. Si no, no se fuerza.',minimum=6),
        make_ticket('Ticket 10 Lottery',10,lotto,'red','Extremo','Se mantiene, pero es de alto riesgo y stake mínimo.',minimum=8)
    ]

def get_dashboard(selected_sports, force_refresh=False):
    d={'mode':'Pro v8 Profit Mode','credit_saving':True,'cache_ttl_seconds':CACHE_TTL,'only_today':ONLY_TODAY,'timezone':LOCAL_TZ,'sports':{},'games':[],'top3':[],'green':[],'blue':[],'red':[],'smart_tickets':[],'warnings':[]}
    for label in selected_sports:
        key=SPORTS.get(label)
        if not key: continue
        try:
            games,from_cache,ttl=fetch_odds(key,force_refresh=force_refresh); total=len(games); games=[g for g in games if is_game_today(g)]
            d['sports'][label]={'ok':True,'sport_key':key,'games_count':len(games),'total_api_games':total,'today_only':ONLY_TODAY,'timezone':LOCAL_TZ,'from_cache':from_cache,'cache_seconds_left':ttl}
            for g in games:
                pred=game_prediction(g,label); d['games'].append(pred)
                for typ in ['primary_pick','secondary_pick']:
                    p=pred.get(typ)
                    if not p: continue
                    item=p.copy(); item.update({'sport':label,'game':pred['game'],'start_time':pred['start_time']})
                    if item['color']=='green': d['green'].append(item)
                    elif item['color']=='blue': d['blue'].append(item)
                    else: d['red'].append(item)
        except Exception as e:
            d['sports'][label]={'ok':False,'sport_key':key,'games_count':0,'error':str(e)}; d['warnings'].append(f'{label}: {e}')
    sorter=lambda x:(x.get('validation',0),x.get('confidence_score',0),x.get('probability',0))
    d['green']=dedupe(sorted(d['green'],key=sorter,reverse=True))[:12]
    d['blue']=dedupe(sorted(d['blue'],key=sorter,reverse=True))[:18]
    d['red']=dedupe(sorted(d['red'],key=sorter,reverse=True))[:15]
    d['top3']=dedupe(d['green']+d['blue'])[:3]
    d['smart_tickets']=build_tickets(d['green'],d['blue'])
    return d

def debug_all_sports():
    r={'api_key':api_key_status(),'regions':REGIONS,'markets':MARKETS,'odds_format':ODDS_FORMAT,'cache_ttl_seconds':CACHE_TTL,'only_today':ONLY_TODAY,'timezone':LOCAL_TZ,'sports':{}}
    for label,key in SPORTS.items():
        try:
            games,fc,ttl=fetch_odds(key); total=len(games); games=[g for g in games if is_game_today(g)]
            r['sports'][label]={'ok':True,'sport_key':key,'games_count':len(games),'total_api_games':total,'today_only':ONLY_TODAY,'timezone':LOCAL_TZ,'from_cache':fc,'cache_seconds_left':ttl,'sample_games':[{'home_team':g.get('home_team'),'away_team':g.get('away_team'),'commence_time':g.get('commence_time'),'bookmakers_count':len(g.get('bookmakers',[]))} for g in games[:5]]}
        except Exception as e: r['sports'][label]={'ok':False,'sport_key':key,'games_count':0,'error':str(e)}
    return r
