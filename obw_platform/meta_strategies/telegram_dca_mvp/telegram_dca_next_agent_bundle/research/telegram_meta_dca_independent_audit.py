from __future__ import annotations
import csv, math, datetime as dt, json, argparse, hashlib
from pathlib import Path
import numpy as np

TP_REACH_PROBS = {
    "edge_in_zone": [0.837, 0.639, 0.518],
    "edge_outside_zone": [0.842, 0.640, 0.489],
    "edge_in_zone_long": [0.815, 0.605, 0.479],
    "edge_in_zone_short": [0.894, 0.723, 0.617],
    "edge_outside_long": [0.838, 0.629, 0.486],
    "edge_outside_short": [0.853, 0.677, 0.500],
}

def parse_dt(s):
    if not s: return None
    s=str(s).strip().replace('Z','+00:00')
    try: return int(dt.datetime.fromisoformat(s).timestamp())
    except Exception: return None

def f(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None

def load_npz(path):
    z=np.load(path, allow_pickle=False)
    syms=[str(x) for x in z['symbols']]
    offs=z['offsets'].astype(np.int64)
    arrays={key:z[key] for key in ['timestamp_s','open','high','low','close','volume']}
    out={}
    for k,s in enumerate(syms):
        a,b=int(offs[k]),int(offs[k+1])
        out[s]={key:arrays[key][a:b] for key in arrays}
    return out

def load_signals(path):
    out=[]
    with open(path, encoding='utf-8-sig', newline='') as fh:
        for r in csv.DictReader(fh):
            side=(r.get('side') or '').upper()
            if side not in ('LONG','SHORT'):
                if side=='LONG'.lower(): side='LONG'
                elif side=='SHORT'.lower(): side='SHORT'
                else: continue
            base=(r.get('symbol') or '').upper().replace('/USDT','').replace(':USDT','')
            vals={k:f(r.get(k)) for k in ['entry_low','entry_high','sl','tp1','tp2','tp3']}
            if vals['entry_low'] is None or vals['entry_high'] is None or vals['sl'] is None: continue
            if vals['entry_low']>vals['entry_high']:
                vals['entry_low'], vals['entry_high'] = vals['entry_high'], vals['entry_low']
            t=parse_dt(r.get('dt_utc') or r.get('datetime') or r.get('timestamp'))
            if not t: continue
            out.append({
                'source_id': str(r.get('message_idx') or r.get('source_id') or len(out)),
                't': t, 'base': base, 'side': side, **vals,
            })
    return sorted(out, key=lambda x:x['t'])

def valid_tp(ev, idx):
    tp=ev.get(f'tp{idx}')
    if tp is None: return None
    if ev['side']=='LONG' and tp <= ev['entry_high']: return None
    if ev['side']=='SHORT' and tp >= ev['entry_low']: return None
    return float(tp)

def sl_hit(side, low, high, sl):
    return low<=sl if side=='LONG' else high>=sl

def tp_hit(side, low, high, tp):
    if tp is None: return False
    return high>=tp if side=='LONG' else low<=tp

def left_zone(low, high, ev):
    return low < ev['entry_low'] or high > ev['entry_high']

def in_zone(close, ev):
    return ev['entry_low'] <= close <= ev['entry_high']

def exec_price(side, action, mark, slip):
    if side=='LONG': return mark*(1+slip) if action=='open' else mark*(1-slip)
    return mark*(1-slip) if action=='open' else mark*(1+slip)

def pnl(side, entry, exit, qty):
    return (exit-entry)*qty if side=='LONG' else (entry-exit)*qty

def tp_return_pct(side, entry, tp):
    if tp is None or entry<=0: return 0.0
    return max(0.0, tp/entry-1) if side=='LONG' else max(0.0, entry/tp-1)

def sl_loss_pct(side, entry, sl):
    if sl is None or entry<=0: return 0.0
    return max(0.0, 1-sl/entry) if side=='LONG' else max(0.0, sl/entry-1)

def weights(ev, entry, preset, exit_at_tp, corrected=False, side_specific=False):
    key=preset
    if side_specific and key=='edge_in_zone':
        key='edge_in_zone_long' if ev['side']=='LONG' else 'edge_in_zone_short'
    probs=TP_REACH_PROBS[key]
    tps=[valid_tp(ev,1),valid_tp(ev,2),valid_tp(ev,3)]
    returns=[tp_return_pct(ev['side'],entry,tp) for tp in tps]
    initial_sl_risk=(1-probs[0])*sl_loss_pct(ev['side'],entry,ev['sl'])
    scores=[max(0,probs[0]*returns[0]-initial_sl_risk), max(0,probs[1]*returns[1]), max(0,probs[2]*returns[2])]
    if corrected:
        for i in range(exit_at_tp,3): scores[i]=0.0
    s=sum(scores)
    if s<=1e-12: return [1,0,0]
    return [x/s for x in scores]

def frac_from_weights(w, tp_idx, exit_at_tp):
    if tp_idx+1 >= exit_at_tp: return 1.0
    rem=1-sum(w[:tp_idx])
    return 1.0 if rem<=1e-12 else max(0.0, min(1.0, w[tp_idx]/rem))

def dca_level(ev, entry_mark, level_no, total, depth=1.0):
    if total<=0: return None
    frac=(level_no/total)*depth
    if ev['side']=='LONG':
        adverse=ev['entry_low']
        if adverse>=entry_mark: return None
    else:
        adverse=ev['entry_high']
        if adverse<=entry_mark: return None
    return entry_mark+(adverse-entry_mark)*frac

def drawdown(equity):
    peak=None; worst=0.0
    for x in equity:
        if peak is None or x>peak: peak=x
        if peak and peak>0: worst=min(worst, x/peak-1)
    return worst

def run(args):
    market=getattr(args, '_market', None) or load_npz(args.npz)
    by_base={s.split('/')[0].upper():s for s in market}
    sigs=getattr(args, '_sigs', None) or load_signals(args.signals)
    equity=args.start_equity
    eq_curve=[equity]
    trades=[]; fills=[]; stats={'missing':0,'no_entry':0,'reject_tp_before':0,'reject_sl_before':0,'reject_late_left_zone':0,'invalid_tp2':0}
    for ev in sigs:
        sym=by_base.get(ev['base'])
        if not sym:
            stats['missing']+=1; continue
        b=market[sym]; ts=b['timestamp_s']
        i0=int(np.searchsorted(ts, ev['t'], side='left'))
        if i0>=len(ts): stats['missing']+=1; continue
        i_end=int(np.searchsorted(ts, ev['t']+int(args.ttl_hours*3600), side='right'))-1
        if i_end<i0: stats['no_entry']+=1; continue
        slc=slice(i0,i_end+1)
        lows=b['low'][slc]; highs=b['high'][slc]; closes=b['close'][slc]; tss=b['timestamp_s'][slc]
        in_zone_arr=(closes>=ev['entry_low']) & (closes<=ev['entry_high'])
        if not np.any(in_zone_arr):
            stats['no_entry']+=1; continue
        left_arr=(lows<ev['entry_low']) | (highs>ev['entry_high'])
        if ev['side']=='LONG':
            sl_arr=lows<=ev['sl']
            tp1=valid_tp(ev,1); tp1_arr=(highs>=tp1) if tp1 is not None else np.zeros_like(in_zone_arr,dtype=bool)
        else:
            sl_arr=highs>=ev['sl']
            tp1=valid_tp(ev,1); tp1_arr=(lows<=tp1) if tp1 is not None else np.zeros_like(in_zone_arr,dtype=bool)
        cum_left=np.maximum.accumulate(left_arr)
        cum_sl=np.maximum.accumulate(sl_arr)
        cum_tp1=np.maximum.accumulate(tp1_arr)
        eligible=in_zone_arr.copy()
        if args.reject_sl_before: eligible &= ~cum_sl
        if not args.allow_late_tp1: eligible &= ~cum_tp1
        late=(tss-ev['t'])>args.hard_ttl
        eligible &= (~late) | (~cum_left)
        idxs=np.flatnonzero(eligible)
        if len(idxs)==0:
            # classify first blocker coarsely
            first_in=int(np.flatnonzero(in_zone_arr)[0])
            if args.reject_sl_before and bool(cum_sl[first_in]): stats['reject_sl_before']+=1
            elif (not args.allow_late_tp1) and bool(cum_tp1[first_in]): stats['reject_tp_before']+=1
            elif bool(late[first_in]) and bool(cum_left[first_in]): stats['reject_late_left_zone']+=1
            else: stats['no_entry']+=1
            continue
        entry_i=i0+int(idxs[0])
        if entry_i is None:
            if not any(v for k,v in stats.items() if False): pass
            continue
        # trade lifecycle
        side=ev['side']; entry_mark=float(b['close'][entry_i]); entry_exec=exec_price(side,'open',entry_mark,args.slip)
        qty=args.notional/max(entry_exec,1e-12); qty0=qty; avg_exec=entry_exec; realized=-args.fee*entry_exec*qty
        tp_done=[False,False,False]; meta_stop=None; meta_dca_done=0; exited=False
        max_notional=args.notional
        for i in range(entry_i+1, len(ts)):
            low=float(b['low'][i]); high=float(b['high'][i]); close=float(b['close'][i]); op=float(b['open'][i])
            active_sl=meta_stop if meta_stop is not None else ev['sl']
            if sl_hit(side,low,high,active_sl):
                exit_mark=float(active_sl); exit_exec=exec_price(side,'close',exit_mark,args.slip)
                realized += pnl(side,avg_exec,exit_exec,qty)-args.fee*exit_exec*qty
                equity += realized; eq_curve.append(equity)
                trades.append({'symbol':sym,'side':side,'pnl':realized,'reason':'meta_stop' if meta_stop is not None else 'sl','entry_t':int(ts[entry_i]),'exit_t':int(ts[i]),'dca':meta_dca_done,'qty0':qty0,'qty_end':qty,'max_notional':max_notional})
                exited=True; break
            meta_touched=False
            for idx in range(3):
                tp=valid_tp(ev,idx+1)
                if tp_done[idx] or tp is None: continue
                if tp_hit(side,low,high,tp):
                    tp_done[idx]=True; meta_touched=True
                    if args.meta_stop:
                        meta_stop=[entry_mark, valid_tp(ev,1), valid_tp(ev,2)][idx]
                    w=weights(ev,entry_mark,args.weights,args.exit_at_tp,args.corrected_weights,args.side_specific_weights)
                    frac=frac_from_weights(w,idx,args.exit_at_tp)
                    if frac>=0.999 or idx+1>=args.exit_at_tp:
                        exit_exec=exec_price(side,'close',tp,args.slip)
                        realized += pnl(side,avg_exec,exit_exec,qty)-args.fee*exit_exec*qty
                        equity += realized; eq_curve.append(equity)
                        trades.append({'symbol':sym,'side':side,'pnl':realized,'reason':f'tp{idx+1}','entry_t':int(ts[entry_i]),'exit_t':int(ts[i]),'dca':meta_dca_done,'qty0':qty0,'qty_end':qty,'max_notional':max_notional})
                        exited=True; break
                    else:
                        qclose=qty*frac; exit_exec=exec_price(side,'close',tp,args.slip)
                        realized += pnl(side,avg_exec,exit_exec,qclose)-args.fee*exit_exec*qclose
                        qty -= qclose
                        fills.append({'symbol':sym,'action':f'tp{idx+1}_partial','t':int(ts[i]),'qty':qclose})
            if exited: break
            if meta_touched: continue
            fills_this=0
            while meta_dca_done < args.dca_adds:
                if args.max_fills_per_bar>0 and fills_this>=args.max_fills_per_bar: break
                lvl=dca_level(ev,entry_mark,meta_dca_done+1,args.dca_adds,args.dca_depth)
                if lvl is None: break
                touched = low<=lvl if side=='LONG' else high>=lvl
                if not touched: break
                fill_mark = lvl
                if args.fill_model=='next_open':
                    if i+1>=len(ts): break
                    fill_mark=float(b['open'][i+1])
                elif args.fill_model=='close':
                    fill_mark=close
                slip=args.slip+args.extra_dca_slip
                add_notional=args.notional*max(0,args.dca_mult-1)/max(1,args.dca_adds)
                add_exec=exec_price(side,'open',fill_mark,slip)
                qadd=add_notional/max(add_exec,1e-12)
                total_cost=avg_exec*qty+add_exec*qadd
                qty += qadd
                avg_exec = total_cost/qty
                realized -= args.fee*add_exec*qadd
                meta_dca_done += 1; fills_this += 1
                max_notional += add_notional
                fills.append({'symbol':sym,'action':'meta_dca','t':int(ts[i]),'level_no':meta_dca_done,'mark':fill_mark,'same_bar_multi':fills_this})
        if not exited:
            i=len(ts)-1; mark=float(b['close'][i]); exit_exec=exec_price(side,'close',mark,args.slip)
            realized += pnl(side,avg_exec,exit_exec,qty)-args.fee*exit_exec*qty
            equity += realized; eq_curve.append(equity)
            trades.append({'symbol':sym,'side':side,'pnl':realized,'reason':'eod','entry_t':int(ts[entry_i]),'exit_t':int(ts[i]),'dca':meta_dca_done,'qty0':qty0,'qty_end':qty,'max_notional':max_notional})
    reasons={}
    for t in trades: reasons[t['reason']]=reasons.get(t['reason'],0)+1
    pnl_vals=[t['pnl'] for t in trades]
    gp=sum(x for x in pnl_vals if x>0); gl=-sum(x for x in pnl_vals if x<0)
    by_sym={}
    for t in trades:
        by_sym.setdefault(t['symbol'],0.0); by_sym[t['symbol']]+=t['pnl']
    return {
        'signals':len(sigs),'trades':len(trades),'pnl_usdt':round(equity-args.start_equity,6),'pnl_pct':round((equity/args.start_equity-1)*100,6),'mdd_pct':round(drawdown(eq_curve)*100,6),'pf':round(gp/gl,4) if gl>0 else None,'reasons':reasons,'stats':stats,
        'dca_fills':sum(1 for x in fills if x['action']=='meta_dca'), 'tp_partials':sum(1 for x in fills if x['action'].endswith('_partial')),
        'multi_dca_bar_events':sum(1 for x in fills if x.get('same_bar_multi',0)>1),
        'by_symbol':{k:round(v,6) for k,v in sorted(by_sym.items())},
        'trades_raw':trades,'fills_raw':fills,
    }

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--npz',required=True); ap.add_argument('--signals',required=True)
    ap.add_argument('--ttl-hours',type=float,default=72); ap.add_argument('--hard-ttl',type=int,default=3600)
    ap.add_argument('--notional',type=float,default=100); ap.add_argument('--start-equity',type=float,default=1000)
    ap.add_argument('--fee',type=float,default=0.0005); ap.add_argument('--slip',type=float,default=0.00092387)
    ap.add_argument('--exit-at-tp',type=int,default=2); ap.add_argument('--weights',default='edge_in_zone')
    ap.add_argument('--corrected-weights',action='store_true'); ap.add_argument('--side-specific-weights',action='store_true')
    ap.add_argument('--dca-adds',type=int,default=0); ap.add_argument('--dca-mult',type=float,default=1.0); ap.add_argument('--dca-depth',type=float,default=1.0)
    ap.add_argument('--max-fills-per-bar',type=int,default=0); ap.add_argument('--fill-model',choices=['touched','close','next_open'],default='touched')
    ap.add_argument('--extra-dca-slip',type=float,default=0.0)
    ap.add_argument('--reject-sl-before',action='store_true',default=True); ap.add_argument('--allow-late-tp1',action='store_true'); ap.add_argument('--meta-stop',action='store_true',default=True)
    args=ap.parse_args()
    res=run(args)
    clean={k:v for k,v in res.items() if not k.endswith('_raw')}
    print(json.dumps(clean,indent=2,sort_keys=True))
