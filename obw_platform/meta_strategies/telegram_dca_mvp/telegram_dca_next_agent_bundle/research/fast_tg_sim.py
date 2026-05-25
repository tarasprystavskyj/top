from __future__ import annotations
import csv, json, math, datetime as dt, argparse
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
    s=s.replace('Z','+00:00')
    return int(dt.datetime.fromisoformat(s).timestamp())

def load_npz(path):
    z=np.load(path, allow_pickle=False)
    syms=[str(x) for x in z['symbols']]
    offs=z['offsets'].astype(np.int64)
    out={}
    for k,s in enumerate(syms):
        a,b=int(offs[k]), int(offs[k+1])
        out[s]={key:z[key][a:b] for key in ['timestamp_s','open','high','low','close','volume']}
    return out

def load_signals(path):
    out=[]
    with open(path,encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            try:
                base=(r.get('symbol') or '').upper().replace('/USDT','').replace(':USDT','')
                side=(r.get('side') or '').upper()
                if side not in ('LONG','SHORT'): continue
                vals={k: float(r.get(k) or 'nan') for k in ['entry_low','entry_high','sl','tp1','tp2','tp3']}
                if not (math.isfinite(vals['entry_low']) and math.isfinite(vals['entry_high']) and math.isfinite(vals['sl'])): continue
                if vals['entry_low']>vals['entry_high']:
                    vals['entry_low'],vals['entry_high']=vals['entry_high'],vals['entry_low']
                for k in ['tp1','tp2','tp3']:
                    if not math.isfinite(vals[k]): vals[k]=None
                out.append(dict(t=parse_dt(r.get('dt_utc') or r.get('datetime') or r.get('timestamp')),base=base,side=side,source_id=str(r.get('message_idx') or r.get('source_id') or ''),**vals))
            except Exception as e:
                pass
    return sorted([x for x in out if x['t']], key=lambda x:x['t'])

def load_exits(path):
    out=[]
    if not path or not Path(path).exists(): return out
    with open(path,encoding='utf-8-sig',newline='') as f:
        for r in csv.DictReader(f):
            t=parse_dt(r.get('dt_utc') or r.get('datetime') or r.get('timestamp'))
            if not t: continue
            base=(r.get('base_symbol') or r.get('symbol') or '').upper().replace('/USDT','').replace(':USDT','')
            out.append(dict(t=t,base=base))
    return sorted(out,key=lambda x:x['t'])

def exec_price(side, action, mark, slip):
    if side=='LONG': return mark*(1+slip) if action=='open' else mark*(1-slip)
    return mark*(1-slip) if action=='open' else mark*(1+slip)

def pnl_close(side, entry_exec, exit_exec, qty):
    return (exit_exec-entry_exec)*qty if side=='LONG' else (entry_exec-exit_exec)*qty

def tp_return(side, entry, tp):
    if tp is None or entry<=0: return 0.0
    return max(0.0, tp/entry-1) if side=='LONG' else max(0.0, entry/tp-1)

def sl_loss(side, entry, sl):
    if sl is None or entry<=0: return 0.0
    return max(0.0,1-sl/entry) if side=='LONG' else max(0.0,sl/entry-1)

def weights(ev, entry, preset, exit_at_tp=2, corrected=False, side_specific=False):
    p=preset
    if side_specific and preset=='edge_in_zone':
        p='edge_in_zone_long' if ev['side']=='LONG' else 'edge_in_zone_short'
    probs=TP_REACH_PROBS[p]
    tps=[ev['tp1'],ev['tp2'],ev['tp3']]
    rets=[tp_return(ev['side'],entry,tp) for tp in tps]
    initial=(1-probs[0])*sl_loss(ev['side'],entry,ev['sl'])
    scores=[max(0,probs[0]*rets[0]-initial), max(0,probs[1]*rets[1]), max(0,probs[2]*rets[2])]
    if corrected:
        for i in range(exit_at_tp,3): scores[i]=0.0
    s=sum(scores)
    if s<=1e-12: return [1,0,0]
    return [x/s for x in scores]

def frac_from_weights(w,tp_idx,exit_at_tp):
    if tp_idx+1>=exit_at_tp: return 1.0
    rem=1-sum(w[:tp_idx])
    if rem<=1e-12: return 1.0
    return max(0,min(1,w[tp_idx]/rem))

def in_zone(block,i,ev):
    c=float(block['close'][i]); return ev['entry_low']<=c<=ev['entry_high']

def left_zone(block,i,ev):
    return float(block['low'][i])<ev['entry_low'] or float(block['high'][i])>ev['entry_high']

def tp_levels_hit(side,block,i,ev):
    out=[]
    for idx,tp in enumerate([ev['tp1'],ev['tp2'],ev['tp3']]):
        if tp is None: continue
        if side=='LONG' and tp<=ev['entry_high']: continue
        if side=='SHORT' and tp>=ev['entry_low']: continue
        if side=='LONG' and float(block['high'][i])>=tp: out.append((idx,tp))
        if side=='SHORT' and float(block['low'][i])<=tp: out.append((idx,tp))
    return out

def sl_hit(side,block,i,sl):
    if sl is None: return False
    return (float(block['low'][i])<=sl) if side=='LONG' else (float(block['high'][i])>=sl)

def dca_level(ev, entry_mark, done_next, total, depth=1.0):
    frac=done_next/total*depth
    if ev['side']=='LONG':
        adverse=ev['entry_low'];
        if adverse>=entry_mark: return None
    else:
        adverse=ev['entry_high'];
        if adverse<=entry_mark: return None
    return entry_mark+(adverse-entry_mark)*frac

def dca_touched(side,block,i,lvl):
    return float(block['low'][i])<=lvl if side=='LONG' else float(block['high'][i])>=lvl

def drawdown(vals):
    pk=-1e100; worst=0
    for v in vals:
        pk=max(pk,v)
        if pk>0: worst=min(worst,v/pk-1)
    return worst

def sim(args):
    market=load_npz(args.npz)
    by_base={s.split('/')[0].replace(':USDT',''):s for s in market}
    # actual symbols look like AAVE/USDT:USDT maybe
    by_base={}
    for s in market:
        base=s.split('/')[0].upper()
        by_base[base]=s
    signals=load_signals(args.signals)
    exits=load_exits(args.events)
    events=[]
    for sig in signals: events.append(('signal',sig['t'],sig))
    for ex in exits: events.append(('exit',ex['t'],ex))
    events.sort(key=lambda x:(x[1],0 if x[0]=='exit' else 1))
    idx_cache={s:{int(t):i for i,t in enumerate(b['timestamp_s'])} for s,b in market.items()}
    active={}; active_order=[]; trades=[]; fills=[]
    equity=args.start_equity; realized_closed=0; curve=[equity]
    waiting=[]; k=0; skipped=0; rejected=0; opened=0
    max_active=8
    cur_t=min([int(b['timestamp_s'][0]) for b in market.values()])
    # event-driven: loop until no events/waiting/active, jump to next min of next event, next active bar, waiting signal eligible next bar approx current t+60
    # Simpler: iterate events and scan active forward to each event time; also process waiting at relevant symbol bars until next event.
    # Given few signals, simulate by chronological global candidate times: all signal times, exit times, and active bars minute-by-minute only when active/waiting.
    event_ptr=0
    # start at first market/event
    if events: cur_t=min(cur_t, events[0][1])
    last_t=cur_t
    # Build market end
    market_end=max(int(b['timestamp_s'][-1]) for b in market.values())
    def close_trade(sym,t,i,mark,reason):
        nonlocal equity, realized_closed
        st=active.pop(sym)
        if sym in active_order: active_order.remove(sym)
        side=st['ev']['side']; qty=st['qty']; avg=st['avg_entry_exec']
        exit_exec=exec_price(side,'close',mark,args.slip)
        pnl=pnl_close(side,avg,exit_exec,qty)-args.fee*exit_exec*qty
        st['realized']+=pnl
        equity+=st['realized']; realized_closed+=st['realized']; curve.append(equity)
        trades.append(dict(symbol=sym,side=side,entry_signal_id=st['ev']['source_id'],entry_t=st['entry_t'],exit_t=t,entry=st['entry_mark'],exit=mark,qty=st['qty0'],pnl=st['realized'],reason=reason,meta_dca_done=st['meta_dca_done']))
    def partial(sym,t,i,mark,frac,reason):
        st=active[sym]; side=st['ev']['side']; q=st['qty']*max(0,min(1,frac))
        if q<=1e-12: return
        exit_exec=exec_price(side,'close',mark,args.slip)
        pnl=pnl_close(side,st['avg_entry_exec'],exit_exec,q)-args.fee*exit_exec*q
        st['realized']+=pnl; st['qty']-=q
        fills.append(dict(t=t,symbol=sym,action=reason,mark=mark,qty=q,pnl=pnl,remaining=st['qty']))
    # We will maintain next bar index for each active/waiting; loop through sorted unique times relevant: all bars for active symbols until next signal. Easier but still light.
    # Use global while with next_t candidates.
    waiting=[]
    while True:
        # determine next time
        cands=[]
        if event_ptr<len(events): cands.append(events[event_ptr][1])
        for sym,st in active.items():
            b=market[sym]; ni=st.get('i',0)+1
            if ni<len(b['timestamp_s']): cands.append(int(b['timestamp_s'][ni]))
        for ev in waiting:
            sym=by_base.get(ev['base'])
            if sym:
                b=market[sym]
                i=np.searchsorted(b['timestamp_s'], max(cur_t, ev['t']), side='left')
                if i<len(b['timestamp_s']): cands.append(int(b['timestamp_s'][i]))
        if not cands: break
        t=min(cands)
        if t>market_end: break
        cur_t=t
        # process exits/signals due into waiting/exits actions before active management? runner does channel exits then manage, then signals
        while event_ptr<len(events) and events[event_ptr][1]<=t and events[event_ptr][0]=='exit':
            ex=events[event_ptr][2]; event_ptr+=1
            if ex['base']:
                close_syms=[s for s,st in active.items() if st['ev']['base']==ex['base']]
            else:
                close_syms=[active_order[-1]] if active_order else []
            for sym in close_syms:
                b=market[sym]; i=np.searchsorted(b['timestamp_s'], t, side='left')
                if i<len(b['timestamp_s']): close_trade(sym,t,int(i),float(b['close'][i]),'channel_exit')
        # if event order signal before exit at same time too complex handle all non-exit later
        # manage active bars at t
        for sym in list(active.keys()):
            st=active.get(sym)
            if st is None: continue
            b=market[sym]
            i=idx_cache[sym].get(t)
            if i is None: continue
            st['i']=i
            ev=st['ev']; side=ev['side']; active_sl=st.get('meta_stop') if st.get('meta_stop') is not None else ev['sl']
            if sl_hit(side,b,i,active_sl):
                close_trade(sym,t,i,float(active_sl),'telegram_meta_stop' if st.get('meta_stop') is not None else 'telegram_sl'); continue
            closed=False; meta_touched=False
            for tp_idx,tp in tp_levels_hit(side,b,i,ev):
                if sym not in active: closed=True; break
                st=active[sym]
                if st['tp_done'][tp_idx]: continue
                st['tp_done'][tp_idx]=True; meta_touched=True
                if args.meta_stop:
                    st['meta_stop']=[st['entry_mark'],ev['tp1'],ev['tp2']][tp_idx]
                w=weights(ev,st['entry_mark'],args.weights,args.exit_at_tp,args.corrected_weights,args.side_specific_weights)
                frac=frac_from_weights(w,tp_idx,args.exit_at_tp)
                if frac>=0.999 or tp_idx+1>=args.exit_at_tp:
                    close_trade(sym,t,i,float(tp),f'telegram_tp{tp_idx+1}'); closed=True; break
                partial(sym,t,i,float(tp),frac,f'telegram_tp{tp_idx+1}_partial')
            if closed or meta_touched: continue
            # meta dca
            fills_this=0
            while st['meta_dca_done']<args.dca_adds:
                if args.max_fills_per_bar and fills_this>=args.max_fills_per_bar: break
                next_no=st['meta_dca_done']+1
                lvl=dca_level(ev,st['entry_mark'],next_no,args.dca_adds,args.dca_depth)
                if lvl is None or not dca_touched(side,b,i,lvl): break
                add_notional=args.notional*max(0,args.dca_mult-1)/max(1,args.dca_adds)
                if add_notional<=0: break
                if args.dca_fill_model=='next_open':
                    if i+1>=len(b['timestamp_s']): break
                    lvl=float(b['open'][i+1])
                qty_add=add_notional/max(lvl,1e-12)
                entry_exec=exec_price(side,'open',lvl,args.slip + args.extra_dca_slip)
                fee=args.fee*entry_exec*qty_add
                # update avg entry exec and realized fee
                total_cost=st['avg_entry_exec']*st['qty']+entry_exec*qty_add
                st['qty']+=qty_add
                st['avg_entry_exec']=total_cost/st['qty']
                st['realized']-=fee
                st['meta_dca_done']+=1; fills_this+=1
                fills.append(dict(t=t,symbol=sym,action='meta_dca',level_no=next_no,mark=lvl,qty=qty_add))
        # process all signals due at t after manage (including those after exits); but if event_ptr points to signal and next exit after same t? handle while due all
        while event_ptr<len(events) and events[event_ptr][1]<=t:
            typ,_,obj=events[event_ptr]; event_ptr+=1
            if typ=='signal': waiting.append(obj)
            elif typ=='exit':
                # late same-time exit after signals due to sorting shouldn't happen because exits ordered first at same t only in list, but ok
                ex=obj
                close_syms=[s for s,st in active.items() if st['ev']['base']==ex['base']] if ex['base'] else ([active_order[-1]] if active_order else [])
                for sym in close_syms:
                    b=market[sym]; i=np.searchsorted(b['timestamp_s'], t, side='left')
                    if i<len(b['timestamp_s']): close_trade(sym,t,int(i),float(b['close'][i]),'channel_exit')
        keep=[]
        for ev in waiting:
            sym=by_base.get(ev['base'])
            if not sym: skipped+=1; continue
            b=market[sym]
            i=idx_cache[sym].get(t)
            if i is None:
                # wait to next symbol bar
                keep.append(ev); continue
            i0=int(np.searchsorted(b['timestamp_s'], ev['t'], side='left'))
            if i0>=len(b['timestamp_s']): rejected+=1; continue
            if int(b['timestamp_s'][i0])-ev['t']>3600: rejected+=1; continue
            if t-ev['t']>args.ttl_hours*3600: rejected+=1; continue
            if args.reject_sl_before and any(sl_hit(ev['side'],b,j,ev['sl']) for j in range(i0,i+1)):
                rejected+=1; continue
            if t-ev['t']>args.hard_ttl:
                stayed=not any(left_zone(b,j,ev) for j in range(i0,i+1))
                if not stayed: rejected+=1; continue
            if sym in active or len(active)>=max_active:
                keep.append(ev); continue
            if not in_zone(b,i,ev):
                keep.append(ev); continue
            if (not args.allow_late_tp1) and any(tp_levels_hit(ev['side'],b,j,ev) for j in range(i0,i+1)):
                rejected+=1; continue
            mark=float(b['close'][i]); entry_exec=exec_price(ev['side'],'open',mark,args.slip)
            qty=args.notional/max(entry_exec,1e-12)
            fee=args.fee*entry_exec*qty
            active[sym]=dict(ev=ev,entry_t=int(b['timestamp_s'][i]),entry_mark=mark,avg_entry_exec=entry_exec,qty=qty,qty0=qty,realized=-fee,tp_done=[False,False,False],meta_stop=None,meta_dca_done=0,i=i)
            active_order.append(sym); opened+=1
            fills.append(dict(t=t,symbol=sym,action='open',mark=mark,qty=qty))
        waiting=keep
        if event_ptr>=len(events) and not active and not waiting: break
    # close active eod
    for sym in list(active.keys()):
        b=market[sym]; i=len(b['timestamp_s'])-1; close_trade(sym,int(b['timestamp_s'][i]),i,float(b['close'][i]),'eod')
    vals=curve
    return dict(signals_total=len(signals),opened=opened,trade_count=len(trades),rejected=rejected,skipped=skipped,start_equity=args.start_equity,end_equity=equity,pnl_pct=equity/args.start_equity-1,mdd_pct=drawdown(vals),pnl_to_mdd=(equity/args.start_equity-1)/abs(drawdown(vals)) if drawdown(vals)<0 else None, dca_fills=sum(1 for f in fills if f['action']=='meta_dca'), tp1_partial=sum(1 for f in fills if f['action']=='telegram_tp1_partial'), reasons={r:sum(1 for t in trades if t['reason']==r) for r in sorted(set(t['reason'] for t in trades))}, fills=fills,trades=trades)

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--npz',required=True); ap.add_argument('--signals',required=True); ap.add_argument('--events',required=True)
    ap.add_argument('--dca-adds',type=int,default=0); ap.add_argument('--dca-mult',type=float,default=1.0); ap.add_argument('--dca-depth',type=float,default=1.0)
    ap.add_argument('--max-fills-per-bar',type=int,default=0); ap.add_argument('--dca-fill-model',choices=['touched','next_open'],default='touched')
    ap.add_argument('--extra-dca-slip',type=float,default=0.0)
    ap.add_argument('--weights',default='edge_in_zone'); ap.add_argument('--corrected-weights',action='store_true'); ap.add_argument('--side-specific-weights',action='store_true')
    ap.add_argument('--exit-at-tp',type=int,default=2); ap.add_argument('--notional',type=float,default=100); ap.add_argument('--start-equity',type=float,default=1000)
    ap.add_argument('--fee',type=float,default=0.0005); ap.add_argument('--slip',type=float,default=0.00092387); ap.add_argument('--ttl-hours',type=float,default=72); ap.add_argument('--hard-ttl',type=int,default=3600)
    ap.add_argument('--reject-sl-before',action='store_true',default=True); ap.add_argument('--allow-late-tp1',action='store_true'); ap.add_argument('--meta-stop',action='store_true',default=True)
    a=ap.parse_args()
    res=sim(a); res2={k:v for k,v in res.items() if k not in ('fills','trades')}
    print(json.dumps(res2,indent=2))
