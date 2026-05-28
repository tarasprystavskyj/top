import csv, json, math, os, re, sys, datetime as dt
from pathlib import Path
from collections import defaultdict, Counter
import numpy as np
import pandas as pd

NPZ='/mnt/data/local_test_tar_bundle/DB/telegram_signals_1m_event_windows_bingx.npz'
SIG='/mnt/data/all_signals_bundle/telegram_signals_extracted.csv'
OUT=Path('/mnt/data/all_signals_fast_reports'); OUT.mkdir(exist_ok=True)
FEE=0.0005; SLIP=0.00092387; START_EQUITY=1000.0; NOTIONAL=100.0

def ts_parse(s):
    return int(pd.Timestamp(s).timestamp())

def side_norm(s): return str(s).upper()

def load_market(npz_path):
    z=np.load(npz_path, allow_pickle=False)
    symbols=[str(x) for x in z['symbols']]
    offs=z['offsets']
    arrays={k:z[k] for k in ['timestamp_s','open','high','low','close','volume']}
    m={}
    for idx,sym in enumerate(symbols):
        a,b=int(offs[idx]),int(offs[idx+1])
        base=sym.split('/')[0]
        m[base]={k:np.array(v[a:b], copy=True) for k,v in arrays.items()}
        m[base]['symbol']=sym
    z.close()
    return m

def load_signals():
    df=pd.read_csv(SIG)
    rows=[]
    for _,r in df.iterrows():
        base=str(r['symbol']).upper()
        side=side_norm(r['side'])
        tp=[float(r['tp1']) if pd.notna(r['tp1']) else None, float(r['tp2']) if pd.notna(r['tp2']) else None, float(r['tp3']) if pd.notna(r['tp3']) else None]
        rows.append(dict(
            source_id=str(int(r['message_idx'])) if pd.notna(r['message_idx']) else str(_),
            t=ts_parse(r['dt_utc']), base=base, side=side,
            entry_low=float(r['entry_low']), entry_high=float(r['entry_high']),
            sl=float(r['sl']) if pd.notna(r['sl']) else None,
            tp=tp, raw_text=str(r.get('raw_text',''))
        ))
    return sorted(rows, key=lambda x:x['t'])

def in_zone(block,i,sig,mode):
    if mode=='first_bar': return True
    if mode=='touch_zone': return bool(block['low'][i] <= sig['entry_high'] and block['high'][i] >= sig['entry_low'])
    return bool(sig['entry_low'] <= block['close'][i] <= sig['entry_high'])

def left_zone(block,i,sig):
    return bool(block['low'][i] < sig['entry_low'] or block['high'][i] > sig['entry_high'])

def sl_hit(side, block, i, sl):
    if sl is None or not math.isfinite(sl): return False
    if side=='LONG': return bool(block['low'][i] <= sl)
    return bool(block['high'][i] >= sl)

def tp_valid(side,tp,sig):
    if tp is None or not math.isfinite(tp): return False
    if side=='LONG': return tp > sig['entry_high']
    return tp < sig['entry_low']

def tp_hit(side, block, i, tp):
    if tp is None or not math.isfinite(tp): return False
    if side=='LONG': return bool(block['high'][i] >= tp)
    return bool(block['low'][i] <= tp)

def tp_levels_hit(side,block,i,sig):
    out=[]
    for k,tp in enumerate(sig['tp']):
        if tp_valid(side,tp,sig) and tp_hit(side,block,i,tp): out.append((k,tp))
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

def current_frac_from_weights(weights,tp_idx,exit_at_tp):
    if tp_idx+1>=exit_at_tp: return 1.0
    remaining=1.0-sum(weights[:tp_idx])
    if remaining<=1e-12: return 1.0
    return max(0.0,min(1.0,weights[tp_idx]/remaining))

def next_meta_stop(sig, entry_mark, tp_idx):
    if tp_idx==0: return entry_mark
    if tp_idx==1: return sig['tp'][0]
    if tp_idx==2: return sig['tp'][1]
    return None

def dca_level(sig, entry_mark, level_no, total):
    if total<=0: return None
    if sig['side']=='LONG':
        adverse=sig['entry_low']
        if adverse>=entry_mark: return None
        return entry_mark + (adverse-entry_mark)*(level_no/(total+1))
    else:
        adverse=sig['entry_high']
        if adverse<=entry_mark: return None
        return entry_mark + (adverse-entry_mark)*(level_no/(total+1))

def dca_touched(sig,block,i,level):
    if sig['side']=='LONG': return bool(block['low'][i] <= level)
    return bool(block['high'][i] >= level)

def simulate_signal(sig, market, mode, exit_at_tp, weights, meta_dca_adds=0, total_mult=1.0):
    ent, reason=find_entry(sig,market,mode=mode)
    if ent is None: return dict(status=reason, sig=sig)
    b=market[sig['base']]; ts=b['timestamp_s']; side=sig['side']
    entry_mark=float(b['close'][ent])
    entry_exec=exec_price(side,'open',entry_mark)
    qty=NOTIONAL/max(entry_exec,1e-12); avg=entry_exec; realized=-open_fee(entry_exec,qty)
    open_qty=qty; tp_done=[False,False,False]; meta_stop=None; dca_done=0; dca_fills=0
    partials=[]; samebar_tp_sl=0; first_tp_before_initial_sl=[False,False,False]; initial_sl_seen=False
    final=None
    add_notional=NOTIONAL*max(0,total_mult-1)/max(1,meta_dca_adds) if meta_dca_adds else 0
    for i in range(ent, len(ts)):
        # record TP-before-initial-SL with stop-first: if SL same bar and not already TP, TP doesn't count before SL
        sl0=sl_hit(side,b,i,sig['sl']) if not initial_sl_seen else False
        tps=tp_levels_hit(side,b,i,sig)
        if sl0:
            initial_sl_seen=True
        if not initial_sl_seen:
            for k,tp in tps: first_tp_before_initial_sl[k]=True
        # same-bar ambiguity with currently active stop and any undone TP
        active_sl=meta_stop if meta_stop is not None else sig['sl']
        sl_now=sl_hit(side,b,i,active_sl)
        tps_undone=[(k,tp) for k,tp in tps if not tp_done[k]]
        if sl_now and tps_undone: samebar_tp_sl += 1
        # stop-first lifecycle
        if sl_now:
            mark=float(active_sl)
            ex=exec_price(side,'close',mark)
            pnl=close_pnl(side,avg,ex,open_qty)
            realized += pnl
            final=dict(status='closed', sig=sig, entry_i=ent, exit_i=i, entry_t=int(ts[ent]), exit_t=int(ts[i]), entry=entry_mark, exit=mark, qty=open_qty, pnl=realized, reason='telegram_meta_stop' if meta_stop is not None else 'telegram_sl', samebar_tp_sl=samebar_tp_sl, tp_before_initial_sl=first_tp_before_initial_sl, partials=partials, dca_fills=dca_fills)
            break
        meta_touched=False
        for k,tp in tps:
            if tp_done[k]: continue
            meta_touched=True; tp_done[k]=True; meta_stop=next_meta_stop(sig,entry_mark,k)
            frac=current_frac_from_weights(weights,k,exit_at_tp)
            if frac>=0.999 or k+1>=exit_at_tp:
                ex=exec_price(side,'close',float(tp)); pnl=close_pnl(side,avg,ex,open_qty); realized+=pnl
                final=dict(status='closed', sig=sig, entry_i=ent, exit_i=i, entry_t=int(ts[ent]), exit_t=int(ts[i]), entry=entry_mark, exit=float(tp), qty=open_qty, pnl=realized, reason=f'telegram_tp{k+1}', samebar_tp_sl=samebar_tp_sl, tp_before_initial_sl=first_tp_before_initial_sl, partials=partials, dca_fills=dca_fills)
                break
            else:
                qclose=open_qty*frac; ex=exec_price(side,'close',float(tp)); pnl=close_pnl(side,avg,ex,qclose); realized+=pnl; open_qty-=qclose
                partials.append(dict(tp=k+1, qty=qclose, pnl=pnl, t=int(ts[i])))
        if final: break
        if meta_touched: continue
        # meta DCA after TP/SL not touched
        while dca_done<meta_dca_adds:
            lvl=dca_level(sig,entry_mark,dca_done+1,meta_dca_adds)
            if lvl is None or not dca_touched(sig,b,i,lvl): break
            open_qty,avg,realized,qadd,exadd=add_position(side,open_qty,avg,realized,float(lvl),add_notional)
            dca_done+=1; dca_fills+=1
            # enforce max one per bar for conservative model
            break
    if final is None:
        i=len(ts)-1; mark=float(b['close'][i]); ex=exec_price(side,'close',mark); realized+=close_pnl(side,avg,ex,open_qty)
        final=dict(status='closed', sig=sig, entry_i=ent, exit_i=i, entry_t=int(ts[ent]), exit_t=int(ts[i]), entry=entry_mark, exit=mark, qty=open_qty, pnl=realized, reason='eod', samebar_tp_sl=samebar_tp_sl, tp_before_initial_sl=first_tp_before_initial_sl, partials=partials, dca_fills=dca_fills)
    return final

def summarize(name, results, total_signals=312):
    closed=[r for r in results if r.get('status')=='closed']
    miss=Counter(r.get('status') for r in results if r.get('status')!='closed')
    eq=START_EQUITY; vals=[]
    for r in closed:
        eq+=r['pnl']; vals.append(eq)
    peak=START_EQUITY; worst=0.0
    for v in vals:
        peak=max(peak,float(v))
        worst=min(worst,float(v)/peak-1.0)
    pnl_pct=(eq-START_EQUITY)/START_EQUITY
    reason=Counter(r['reason'] for r in closed)
    samebar=sum(1 for r in closed if r.get('samebar_tp_sl',0)>0)
    tp_before=[sum(1 for r in closed if r['tp_before_initial_sl'][k]) for k in range(3)]
    part=Counter()
    part_pnl=Counter()
    for r in closed:
        for p in r.get('partials',[]):
            part[f"tp{p['tp']}_partial"]+=1; part_pnl[f"tp{p['tp']}_partial"]+=p['pnl']
    out=dict(name=name, signals_total=total_signals, opened=len(closed), trades=len(closed), missing=miss.get('missing',0), rejected=sum(v for k,v in miss.items() if str(k).startswith('reject') or str(k)=='no_entry_ttl'), skipped=dict(miss), pnl_pct=pnl_pct, mdd_pct=worst, pnl_to_mdd=(pnl_pct/abs(worst) if worst<0 else None), reason_breakdown=dict(reason), partial_breakdown=dict(part), partial_pnl=dict(part_pnl), tp_before_initial_sl_counts={'tp1':tp_before[0],'tp2':tp_before[1],'tp3':tp_before[2]}, samebar_tp_sl_trades=samebar, samebar_tp_sl_events=sum(r.get('samebar_tp_sl',0) for r in closed), meta_dca_fills=sum(r.get('dca_fills',0) for r in closed))
    with open(OUT/f'{name}_summary.json','w') as f: json.dump(out,f,indent=2)
    rows=[]
    for r in closed:
        rows.append({k:r.get(k) for k in ['status','entry_t','exit_t','entry','exit','qty','pnl','reason','samebar_tp_sl','dca_fills']} | {'source_id':r['sig']['source_id'],'base':r['sig']['base'],'side':r['sig']['side']})
    pd.DataFrame(rows).to_csv(OUT/f'{name}_trades.csv',index=False)
    return out

if __name__=='__main__':
    market=load_market(NPZ); signals=load_signals()
    variants=[]
    variants.append(('no_events_fixed_thirds_close', 'close_in_zone', 3, [1/3,1/3,1/3],0,1.0))
    variants.append(('no_events_tp2_50_close', 'close_in_zone', 2, [0.5,0.5,0.0],0,1.0))
    variants.append(('touch_fixed_thirds_no_events', 'touch_zone', 3, [1/3,1/3,1/3],0,1.0))
    variants.append(('dca_no_events_tp2_50_1add_1p5x', 'close_in_zone', 2, [0.5,0.5,0.0],1,1.5))
    variants.append(('dca_no_events_tp2_50_2add_2p0x', 'close_in_zone', 2, [0.5,0.5,0.0],2,2.0))
    variants.append(('dca_no_events_fixed_thirds_1add_1p5x', 'close_in_zone', 3, [1/3,1/3,1/3],1,1.5))
    variants.append(('dca_no_events_fixed_thirds_2add_2p0x', 'close_in_zone', 3, [1/3,1/3,1/3],2,2.0))
    summaries=[]
    for name,mode,exit_at,weights,adds,mult in variants:
        res=[simulate_signal(s,market,mode,exit_at,weights,adds,mult) for s in signals]
        summaries.append(summarize(name,res,len(signals)))
        print(json.dumps(summaries[-1], indent=2))
    pd.DataFrame(summaries).to_csv(OUT/'all_fast_summaries.csv',index=False)
