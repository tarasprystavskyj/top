import math, json, time, os
from pathlib import Path
from collections import Counter, defaultdict, deque
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
NPZ=Path(os.environ.get('TG_NPZ', ROOT/'local_test_bundle/DB/telegram_signals_1m_event_windows_bingx.npz'))
SIG=Path(os.environ.get('TG_SIGNALS', ROOT/'local_test_bundle/DB/telegram_signal_standard_bt/telegram_signals_extracted.csv'))
MFE=Path(os.environ.get('TG_MFE', ROOT/'reports/all_signals_mfe_vs_tp1_rows.csv'))
OUT=Path(os.environ.get('TG_OUT', ROOT/'reports/strategy_versions_research_rerun'))
OUT.mkdir(exist_ok=True)
FEE=0.0005
SLIP=0.00092387
START_EQUITY=1000.0
NOTIONAL=100.0

# ---------- load ----------
def ts_parse(s):
    return int(pd.Timestamp(s).timestamp())

def side_norm(s): return str(s).upper()

def load_market(npz_path=NPZ):
    z=np.load(npz_path, allow_pickle=False)
    symbols=[str(x) for x in z['symbols']]
    offs=z['offsets']
    arrays={k:z[k] for k in ['timestamp_s','open','high','low','close','volume']}
    m={}
    for idx,sym in enumerate(symbols):
        a,b=int(offs[idx]),int(offs[idx+1])
        base=sym.split('/')[0].upper()
        m[base]={k:np.array(v[a:b], copy=True) for k,v in arrays.items()}
        m[base]['symbol']=sym
    z.close()
    return m

def load_signals():
    df=pd.read_csv(SIG)
    mfe=pd.read_csv(MFE)
    mfemap={int(r.message_idx):r for _,r in mfe.iterrows() if str(r.get('status'))=='ok'}
    rows=[]
    for idx,r in df.iterrows():
        base=str(r['symbol']).upper(); side=side_norm(r['side'])
        tp=[float(r['tp1']) if pd.notna(r['tp1']) else None, float(r['tp2']) if pd.notna(r['tp2']) else None, float(r['tp3']) if pd.notna(r['tp3']) else None]
        mid=float(r['entry_mid'])
        tp1_dist=abs(tp[0]-mid)/mid if tp[0] and math.isfinite(tp[0]) else float('nan')
        sl_dist=abs(mid-float(r['sl']))/mid if pd.notna(r['sl']) else float('nan')
        msg=int(r['message_idx']) if pd.notna(r['message_idx']) else idx
        mm=mfemap.get(msg)
        rows.append(dict(
            source_id=str(msg), msg=msg, t=ts_parse(r['dt_utc']), dt=str(r['dt_utc']), base=base, symbol=f'{base}/USDT:USDT', side=side,
            entry_low=float(r['entry_low']), entry_high=float(r['entry_high']), entry_mid=mid,
            sl=float(r['sl']) if pd.notna(r['sl']) else None,
            tp=tp, zone_pct=float(r['zone_pct']) if pd.notna(r['zone_pct']) else float('nan'),
            tp1_pct=float(r['tp1_pct']) if pd.notna(r['tp1_pct']) else tp1_dist,
            sl_extra_pct=float(r['sl_extra_pct']) if pd.notna(r['sl_extra_pct']) else sl_dist,
            rr_tp1_sl=(tp1_dist/sl_dist if sl_dist and math.isfinite(sl_dist) and sl_dist>0 else float('nan')),
            mfe24=(float(mm['mfe_vs_tp1_h24_pct']) if mm is not None and pd.notna(mm['mfe_vs_tp1_h24_pct']) else float('nan')),
            mfe72=(float(mm['mfe_vs_tp1_h72_pct']) if mm is not None and pd.notna(mm['mfe_vs_tp1_h72_pct']) else float('nan')),
            hit24=(bool(mm['tp1_reached_h24']) if mm is not None and pd.notna(mm['tp1_reached_h24']) else False),
            hit72=(bool(mm['tp1_reached_h72']) if mm is not None and pd.notna(mm['tp1_reached_h72']) else False),
        ))
    return sorted(rows, key=lambda x:x['t'])

# ---------- market helpers ----------
def in_zone(block,i,sig,mode):
    if mode=='first_bar': return True
    if mode=='touch_zone': return bool(block['low'][i] <= sig['entry_high'] and block['high'][i] >= sig['entry_low'])
    return bool(sig['entry_low'] <= block['close'][i] <= sig['entry_high'])

def left_zone(block,i,sig):
    return bool(block['low'][i] < sig['entry_low'] or block['high'][i] > sig['entry_high'])

def sl_hit(side, block, i, sl):
    if sl is None or not math.isfinite(sl): return False
    return bool(block['low'][i] <= sl) if side=='LONG' else bool(block['high'][i] >= sl)

def tp_valid(side,tp,sig):
    if tp is None or not math.isfinite(tp): return False
    return tp > sig['entry_high'] if side=='LONG' else tp < sig['entry_low']

def price_hit(side, block, i, price):
    if price is None or not math.isfinite(price): return False
    return bool(block['high'][i] >= price) if side=='LONG' else bool(block['low'][i] <= price)

def tp_levels_hit(side,block,i,sig):
    out=[]
    for k,tp in enumerate(sig['tp']):
        if tp_valid(side,tp,sig) and price_hit(side,block,i,tp): out.append((k,tp))
    return out

def exec_price(side, action, mark):
    if side=='LONG': return mark*(1+SLIP) if action=='open' else mark*(1-SLIP)
    return mark*(1-SLIP) if action=='open' else mark*(1+SLIP)

def close_pnl(side, avg_entry_exec, exit_exec, qty):
    gross=(exit_exec-avg_entry_exec)*qty if side=='LONG' else (avg_entry_exec-exit_exec)*qty
    return gross - FEE*exit_exec*qty

def open_fee(exec_px, qty): return FEE*exec_px*qty

def add_position(side, qty, avg_exec, realized, mark, notional):
    ex=exec_price(side,'open',mark)
    q=notional/max(ex,1e-12)
    newqty=qty+q
    newavg=(avg_exec*qty + ex*q)/max(newqty,1e-12) if qty>0 else ex
    return newqty,newavg,realized-open_fee(ex,q),q,ex

def find_entry(sig, market, mode='close_in_zone', ttl_h=72, hard_ttl=3600, reject_sl=True, reject_tp1=True):
    block=market.get(sig['base'])
    if block is None: return None, 'missing'
    ts=block['timestamp_s']; i0=int(np.searchsorted(ts, sig['t'], side='left'))
    if i0>=len(ts): return None,'stale'
    if int(ts[i0])-sig['t']>3600: return None,'stale'
    deadline=sig['t']+int(ttl_h*3600)
    pre_sl=False; pre_tp=False; stayed=True
    i=i0
    while i<len(ts) and int(ts[i])<=deadline:
        if reject_sl and sl_hit(sig['side'],block,i,sig['sl']): pre_sl=True
        if reject_tp1 and any(tp_levels_hit(sig['side'],block,i,sig)): pre_tp=True
        if left_zone(block,i,sig): stayed=False
        if int(ts[i])-sig['t']>hard_ttl and not stayed:
            return None,'reject_late_left_zone'
        if in_zone(block,i,sig,mode):
            if pre_sl: return None,'reject_sl_before_entry'
            if pre_tp: return None,'reject_tp_before_entry'
            return i,'open'
        i+=1
    return None,'no_entry_ttl'

# ---------- strategy helpers ----------
def target_from_tp1_frac(sig, entry_mark, frac):
    tp1=sig['tp'][0]
    if tp1 is None or not math.isfinite(tp1): return None
    return entry_mark + frac*(tp1-entry_mark)

def stop_price(sig, entry_mark, mode):
    side=sig['side']
    if mode=='telegram_sl': return sig['sl']
    if mode=='zone_edge': return sig['entry_low'] if side=='LONG' else sig['entry_high']
    if mode=='half_zone_to_sl':
        if sig['sl'] is None or not math.isfinite(sig['sl']): return None
        adverse=sig['entry_low'] if side=='LONG' else sig['entry_high']
        return (adverse+sig['sl'])/2.0
    if mode=='none': return None
    raise ValueError(mode)

def dca_level(sig, entry_mark, level_no, total, depth=1.0):
    if total<=0: return None
    adverse=sig['entry_low'] if sig['side']=='LONG' else sig['entry_high']
    if sig['side']=='LONG' and adverse>=entry_mark: return None
    if sig['side']=='SHORT' and adverse<=entry_mark: return None
    return entry_mark + (adverse-entry_mark)*depth*(level_no/(total+1))

def dca_touched(sig, block, i, level):
    return bool(block['low'][i] <= level) if sig['side']=='LONG' else bool(block['high'][i] >= level)

def breakeven_mark(side, avg_exec, qty, realized):
    if qty<=0: return None
    if side=='LONG':
        return (avg_exec*qty - realized)/(qty*(1-SLIP)*(1-FEE))
    else:
        return (realized + avg_exec*qty)/(qty*(1+SLIP)*(1+FEE))

def liq_hit_1x(side, block, i, avg_exec):
    # coarse isolated 1x liquidation model; long near zero, short near 2x entry
    if side=='LONG': return bool(block['low'][i] <= avg_exec*0.05)
    return bool(block['high'][i] >= avg_exec*1.95)

def simulate_target(sig, market, target_frac=0.5, stop_mode='zone_edge', timeout_h=24, mode='close_in_zone', progress=None):
    ent, reason=find_entry(sig,market,mode=mode)
    if ent is None: return dict(status=reason, sig=sig)
    b=market[sig['base']]; ts=b['timestamp_s']; side=sig['side']
    entry_mark=float(b['close'][ent]); entry_exec=exec_price(side,'open',entry_mark)
    qty=NOTIONAL/max(entry_exec,1e-12); realized=-open_fee(entry_exec,qty)
    tgt=target_from_tp1_frac(sig, entry_mark, target_frac)
    stp=stop_price(sig, entry_mark, stop_mode)
    deadline=int(ts[ent]) + int(timeout_h*3600) if timeout_h is not None else None
    # progress=(hours, min_frac_to_tp1) close if not reached target progress by checkpoint
    progress_deadline=None; progress_price=None
    if progress:
        progress_deadline=int(ts[ent])+int(progress[0]*3600)
        progress_price=target_from_tp1_frac(sig,entry_mark,progress[1])
    mae=0.0; mfe=0.0; samebar=0
    for i in range(ent,len(ts)):
        # same bar target and stop: stop-first
        stop_now=sl_hit(side,b,i,stp) if stp is not None else False
        target_now=price_hit(side,b,i,tgt)
        if stop_now and target_now: samebar+=1
        if stop_now:
            ex=exec_price(side,'close',float(stp)); realized+=close_pnl(side,entry_exec,ex,qty)
            return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason=f'stop_{stop_mode}',entry=entry_mark,exit=float(stp),samebar=samebar,hold_h=(int(ts[i])-int(ts[ent]))/3600)
        if target_now:
            ex=exec_price(side,'close',float(tgt)); realized+=close_pnl(side,entry_exec,ex,qty)
            return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason=f'target_{target_frac:g}tp1',entry=entry_mark,exit=float(tgt),samebar=samebar,hold_h=(int(ts[i])-int(ts[ent]))/3600)
        if progress_deadline and int(ts[i])>=progress_deadline:
            # if progress price not reached before checkpoint, exit at close
            if not price_hit(side,b,i,progress_price):
                mark=float(b['close'][i]); ex=exec_price(side,'close',mark); realized+=close_pnl(side,entry_exec,ex,qty)
                return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason=f'progress_fail_{progress[0]}h_{progress[1]}tp1',entry=entry_mark,exit=mark,samebar=samebar,hold_h=(int(ts[i])-int(ts[ent]))/3600)
            progress_deadline=None
        if deadline and int(ts[i])>=deadline:
            mark=float(b['close'][i]); ex=exec_price(side,'close',mark); realized+=close_pnl(side,entry_exec,ex,qty)
            return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason=f'timeout_{timeout_h}h',entry=entry_mark,exit=mark,samebar=samebar,hold_h=(int(ts[i])-int(ts[ent]))/3600)
    i=len(ts)-1; mark=float(b['close'][i]); ex=exec_price(side,'close',mark); realized+=close_pnl(side,entry_exec,ex,qty)
    return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason='eod',entry=entry_mark,exit=mark,samebar=samebar,hold_h=(int(ts[i])-int(ts[ent]))/3600)

def simulate_no_sl_recovery(sig, market, profit_frac=0.0, meta_dca_adds=0, total_mult=1.0, depth=1.0, max_hold_h=24*30, mode='close_in_zone'):
    ent, reason=find_entry(sig,market,mode=mode)
    if ent is None: return dict(status=reason, sig=sig)
    b=market[sig['base']]; ts=b['timestamp_s']; side=sig['side']
    entry_mark=float(b['close'][ent]); entry_exec=exec_price(side,'open',entry_mark)
    qty=NOTIONAL/max(entry_exec,1e-12); avg=entry_exec; realized=-open_fee(entry_exec,qty)
    add_notional=NOTIONAL*max(0,total_mult-1)/max(1,meta_dca_adds) if meta_dca_adds else 0
    dca_done=0; dca_fills=0; max_notional=NOTIONAL
    deadline=int(ts[ent])+int(max_hold_h*3600) if max_hold_h else None
    liq=False
    for i in range(ent,len(ts)):
        if liq_hit_1x(side,b,i,avg):
            mark=avg*0.05 if side=='LONG' else avg*1.95
            ex=exec_price(side,'close',mark); realized+=close_pnl(side,avg,ex,qty)
            return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason='liquidation_1x',entry=entry_mark,exit=mark,dca_fills=dca_fills,hold_h=(int(ts[i])-int(ts[ent]))/3600,max_notional=max_notional)
        # target: fee/slip-aware BE plus optional fraction of original TP1 distance
        be=breakeven_mark(side,avg,qty,realized)
        base_target=target_from_tp1_frac(sig,entry_mark,profit_frac)
        if profit_frac<=0 or base_target is None:
            target=be
        else:
            # require at least breakeven; then add profit target in signal direction if it is beyond BE
            target=base_target
            if side=='LONG': target=max(target,be)
            else: target=min(target,be)
        if price_hit(side,b,i,target):
            ex=exec_price(side,'close',target); realized+=close_pnl(side,avg,ex,qty)
            return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason=f'recovery_{profit_frac:g}tp1',entry=entry_mark,exit=target,dca_fills=dca_fills,hold_h=(int(ts[i])-int(ts[ent]))/3600,max_notional=max_notional)
        # resting DCA, max 1 fill/bar
        if dca_done<meta_dca_adds:
            lvl=dca_level(sig,entry_mark,dca_done+1,meta_dca_adds,depth=depth)
            if lvl is not None and dca_touched(sig,b,i,lvl):
                qty,avg,realized,qadd,exadd=add_position(side,qty,avg,realized,float(lvl),add_notional)
                dca_done+=1; dca_fills+=1
                max_notional += add_notional
        if deadline and int(ts[i])>=deadline:
            mark=float(b['close'][i]); ex=exec_price(side,'close',mark); realized+=close_pnl(side,avg,ex,qty)
            return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason=f'max_hold_{max_hold_h/24:g}d',entry=entry_mark,exit=mark,dca_fills=dca_fills,hold_h=(int(ts[i])-int(ts[ent]))/3600,max_notional=max_notional)
    i=len(ts)-1; mark=float(b['close'][i]); ex=exec_price(side,'close',mark); realized+=close_pnl(side,avg,ex,qty)
    return dict(status='closed',sig=sig,entry_t=int(ts[ent]),exit_t=int(ts[i]),pnl=realized,reason='eod',entry=entry_mark,exit=mark,dca_fills=dca_fills,hold_h=(int(ts[i])-int(ts[ent]))/3600,max_notional=max_notional)

# ---------- filters and walk-forward scale ----------
def build_prior_stats(signals):
    # prior stats by symbol+side using actual historical MFE outcomes only before each signal
    hist=defaultdict(list)
    out={}
    for s in signals:
        key=(s['base'],s['side'])
        arr=hist[key]
        out[s['msg']]={
            'prior_n': len(arr),
            'prior_med_mfe24': float(np.median([x['mfe24'] for x in arr])) if arr else float('nan'),
            'prior_med_mfe72': float(np.median([x['mfe72'] for x in arr])) if arr else float('nan'),
            'prior_hit24': float(np.mean([x['hit24'] for x in arr])) if arr else float('nan'),
            'prior_hit72': float(np.mean([x['hit72'] for x in arr])) if arr else float('nan'),
        }
        if math.isfinite(s.get('mfe24',float('nan'))):
            arr.append(s)
    return out

def scale_from_med(med):
    # coarse bins; not tuned at symbol level
    if not math.isfinite(med): return 0.5
    if med < 90: return 0.5
    if med < 110: return 0.75
    if med < 140: return 1.0
    return 1.25

def filter_signals(signals, name, prior=None):
    out=[]
    for s in signals:
        ok=True
        if name=='all': ok=True
        elif name=='short_only': ok=s['side']=='SHORT'
        elif name=='long_only': ok=s['side']=='LONG'
        elif name=='rr_tp1_sl_ge_0p75': ok=math.isfinite(s['rr_tp1_sl']) and s['rr_tp1_sl']>=0.75
        elif name=='rr_tp1_sl_ge_1p0': ok=math.isfinite(s['rr_tp1_sl']) and s['rr_tp1_sl']>=1.0
        elif name=='zone_pct_le_8pct': ok=math.isfinite(s['zone_pct']) and s['zone_pct']<=0.08
        elif name=='short_or_rr_ge_1': ok=(s['side']=='SHORT') or (math.isfinite(s['rr_tp1_sl']) and s['rr_tp1_sl']>=1.0)
        elif name=='wf_sym_side_prior3_hit72_ge_0p67':
            p=prior[s['msg']]; ok=p['prior_n']>=3 and p['prior_hit72']>=0.67
        elif name=='wf_sym_side_prior5_hit72_ge_0p70':
            p=prior[s['msg']]; ok=p['prior_n']>=5 and p['prior_hit72']>=0.70
        elif name=='wf_sym_side_prior3_med24_ge_100':
            p=prior[s['msg']]; ok=p['prior_n']>=3 and p['prior_med_mfe24']>=100
        elif name=='wf_sym_side_prior5_med24_ge_100':
            p=prior[s['msg']]; ok=p['prior_n']>=5 and p['prior_med_mfe24']>=100
        else: raise ValueError(name)
        if ok: out.append(s)
    return out

def summarize(name, results, selected_n, total_n=312):
    closed=[r for r in results if r.get('status')=='closed']
    miss=Counter(r.get('status') for r in results if r.get('status')!='closed')
    rows=[]
    for r in closed:
        rows.append(dict(
            name=name, msg=r['sig']['msg'], symbol=r['sig']['symbol'], side=r['sig']['side'],
            entry_t=pd.to_datetime(r['entry_t'],unit='s',utc=True).isoformat(), exit_t=pd.to_datetime(r['exit_t'],unit='s',utc=True).isoformat(),
            pnl=r['pnl'], reason=r['reason'], hold_h=r.get('hold_h'), dca_fills=r.get('dca_fills',0), max_notional=r.get('max_notional',NOTIONAL)
        ))
    df=pd.DataFrame(rows)
    if not df.empty:
        df=df.sort_values('exit_t')
    eq=START_EQUITY; peak=START_EQUITY; worst=0
    vals=[]
    for pnl in (df['pnl'].tolist() if not df.empty else []):
        eq += pnl; vals.append(eq); peak=max(peak,eq); worst=min(worst,eq/peak-1)
    pnl_pct=(eq-START_EQUITY)/START_EQUITY
    out=dict(
        name=name,total_signals=total_n,selected=selected_n,opened=len(closed),
        skipped_pre_filter=total_n-selected_n,missing=miss.get('missing',0),rejected=sum(v for k,v in miss.items() if str(k).startswith('reject') or k=='no_entry_ttl'),
        pnl_pct=pnl_pct*100,mdd_pct=worst*100,pnl_to_mdd=(pnl_pct/abs(worst) if worst<0 else None),
        avg_pnl=(float(df['pnl'].mean()) if not df.empty else 0),median_pnl=(float(df['pnl'].median()) if not df.empty else 0),
        worst_trade=(float(df['pnl'].min()) if not df.empty else 0),best_trade=(float(df['pnl'].max()) if not df.empty else 0),
        median_hold_h=(float(df['hold_h'].median()) if not df.empty and 'hold_h' in df else None),p95_hold_h=(float(df['hold_h'].quantile(.95)) if not df.empty and 'hold_h' in df else None),
        max_capital_per_trade=(float(df['max_notional'].max()) if not df.empty and 'max_notional' in df else NOTIONAL),
        reason_breakdown=dict(Counter(df['reason'].tolist())) if not df.empty else {},
        dca_fills=int(df['dca_fills'].sum()) if not df.empty and 'dca_fills' in df else 0,
    )
    # symbol concentration: gross positive PnL by symbol
    if not df.empty:
        bysym=df.groupby('symbol')['pnl'].sum().sort_values(ascending=False)
        out['top_symbol_pnl']=bysym.head(3).to_dict()
        out['bottom_symbol_pnl']=bysym.tail(3).to_dict()
        pos_total=bysym[bysym>0].sum()
        out['top_symbol_pos_share']=float(bysym.iloc[0]/pos_total) if pos_total>0 and bysym.iloc[0]>0 else None
    return out,df

# ---------- run variants ----------
def run():
    t0=time.time()
    market=load_market(); signals=load_signals(); prior=build_prior_stats(signals)
    summaries=[]; all_trades=[]
    filter_names=['all','short_only','long_only','rr_tp1_sl_ge_0p75','rr_tp1_sl_ge_1p0','zone_pct_le_8pct','short_or_rr_ge_1','wf_sym_side_prior3_hit72_ge_0p67','wf_sym_side_prior5_hit72_ge_0p70','wf_sym_side_prior3_med24_ge_100','wf_sym_side_prior5_med24_ge_100']
    # Structural target tests
    target_setups=[]
    for frac in [0.25,0.5,0.75,1.0]:
        target_setups.append((f'impulse_{frac:g}tp1_zone_stop_24h', dict(target_frac=frac, stop_mode='zone_edge', timeout_h=24, progress=None)))
    for frac in [0.25,0.5,0.75,1.0]:
        target_setups.append((f'impulse_{frac:g}tp1_halfsl_stop_24h', dict(target_frac=frac, stop_mode='half_zone_to_sl', timeout_h=24, progress=None)))
    target_setups.append(('progress_1h_25pct_then_0p5tp1_zone_24h', dict(target_frac=0.5, stop_mode='zone_edge', timeout_h=24, progress=(1,0.25))))
    target_setups.append(('progress_6h_50pct_then_1tp1_zone_48h', dict(target_frac=1.0, stop_mode='zone_edge', timeout_h=48, progress=(6,0.5))))
    # Run filters for a selected small set of robust setups
    for setup_name,kw in target_setups:
        # all only for every setup
        selected=filter_signals(signals,'all',prior)
        res=[simulate_target(s,market,**kw) for s in selected]
        summ,df=summarize(setup_name+'__all',res,len(selected),len(signals)); summaries.append(summ); all_trades.append(df)
    # selected filters on best-looking simple target families to avoid huge run count
    filter_test_setups=[
        ('impulse_0p25tp1_zone_stop_24h', dict(target_frac=0.25, stop_mode='zone_edge', timeout_h=24, progress=None)),
        ('impulse_0p5tp1_zone_stop_24h', dict(target_frac=0.5, stop_mode='zone_edge', timeout_h=24, progress=None)),
        ('impulse_1tp1_zone_stop_24h', dict(target_frac=1.0, stop_mode='zone_edge', timeout_h=24, progress=None)),
    ]
    for filt in filter_names[1:]:
        selected=filter_signals(signals,filt,prior)
        for setup_name,kw in filter_test_setups:
            res=[simulate_target(s,market,**kw) for s in selected]
            summ,df=summarize(setup_name+'__filter_'+filt,res,len(selected),len(signals)); summaries.append(summ); all_trades.append(df)
    # TP scale models: in-sample upper bound and walk-forward coarse bins
    # in-sample by symbol+side based on full MFE median h24, min n>=5 else default 0.5
    mf=pd.DataFrame(signals)
    full_med=mf.groupby(['base','side']).agg(n=('msg','size'), med24=('mfe24','median'), med72=('mfe72','median')).reset_index()
    full_scale={(r.base,r.side): scale_from_med(float(r.med24)) if int(r.n)>=5 else 0.5 for _,r in full_med.iterrows()}
    # simulate custom per-signal frac
    def sim_scaled(selected, mode_name):
        results=[]
        for s in selected:
            if mode_name=='insample_symbol_side_med24':
                frac=full_scale.get((s['base'],s['side']),0.5)
            elif mode_name=='wf_symbol_side_med24_prior3':
                p=prior[s['msg']]
                if p['prior_n']<3:
                    # skip until enough prior observations; avoids default noise
                    results.append(dict(status='skip_no_prior',sig=s)); continue
                frac=scale_from_med(p['prior_med_mfe24'])
            elif mode_name=='wf_symbol_side_med24_prior5':
                p=prior[s['msg']]
                if p['prior_n']<5:
                    results.append(dict(status='skip_no_prior',sig=s)); continue
                frac=scale_from_med(p['prior_med_mfe24'])
            else: raise ValueError(mode_name)
            results.append(simulate_target(s,market,target_frac=frac,stop_mode='zone_edge',timeout_h=24))
        return results
    for mode_name in ['insample_symbol_side_med24','wf_symbol_side_med24_prior3','wf_symbol_side_med24_prior5']:
        selected=signals
        res=sim_scaled(selected,mode_name)
        eff_selected=sum(1 for r in res if r.get('status')!='skip_no_prior')
        summ,df=summarize('tp_scale_'+mode_name+'__zone_stop_24h',res,eff_selected,len(signals)); summaries.append(summ); all_trades.append(df)
    # No hard SL BE recovery tests
    recovery_setups=[]
    for frac in [0.0,0.25,0.5]:
        recovery_setups.append((f'recovery_noSL_{frac:g}tp1_noDCA_30d', dict(profit_frac=frac,meta_dca_adds=0,total_mult=1.0,max_hold_h=24*30)))
        recovery_setups.append((f'recovery_noSL_{frac:g}tp1_1add1p5x_30d', dict(profit_frac=frac,meta_dca_adds=1,total_mult=1.5,max_hold_h=24*30)))
    for name,kw in recovery_setups:
        selected=signals
        res=[simulate_no_sl_recovery(s,market,**kw) for s in selected]
        summ,df=summarize(name+'__all',res,len(selected),len(signals)); summaries.append(summ); all_trades.append(df)
    # Save
    sdf=pd.DataFrame(summaries)
    sdf=sdf.sort_values(['pnl_to_mdd','pnl_pct'], ascending=[False,False])
    sdf.to_csv(OUT/'strategy_versions_summary.csv',index=False)
    if all_trades:
        tdf=pd.concat([df for df in all_trades if not df.empty], ignore_index=True)
        tdf.to_csv(OUT/'strategy_versions_trades.csv',index=False)
    with open(OUT/'strategy_versions_summary.json','w') as f: json.dump(summaries,f,indent=2)
    print('done',len(summaries),'variants', 'time',time.time()-t0)
    print(sdf[['name','selected','opened','pnl_pct','mdd_pct','pnl_to_mdd','worst_trade','median_hold_h','p95_hold_h','dca_fills']].head(30).to_string(index=False))

if __name__=='__main__':
    run()
