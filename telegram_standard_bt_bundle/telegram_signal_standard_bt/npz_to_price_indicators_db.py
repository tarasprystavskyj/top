#!/usr/bin/env python3
from __future__ import annotations
import argparse, sqlite3
from datetime import datetime, timezone
import numpy as np


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--npz', required=True)
    ap.add_argument('--out-db', required=True)
    ap.add_argument('--replace', action='store_true')
    args=ap.parse_args()
    z=np.load(args.npz, allow_pickle=False)
    syms=[str(x) for x in z['symbols']]
    off=z['offsets']
    ts=z['timestamp_s']; op=z['open']; hi=z['high']; lo=z['low']; cl=z['close']; vol=z['volume']
    con=sqlite3.connect(args.out_db)
    cur=con.cursor()
    if args.replace:
        cur.execute('drop table if exists price_indicators')
    cur.execute('''create table if not exists price_indicators(
        symbol text not null,
        datetime_utc text not null,
        open real,
        high real,
        low real,
        close real,
        volume real,
        quote_volume real,
        primary key(symbol, datetime_utc)
    )''')
    cur.execute('create index if not exists idx_price_indicators_dt_sym on price_indicators(datetime_utc, symbol)')
    batch=[]; total=0
    for i,sym in enumerate(syms):
        a=int(off[i]); b=int(off[i+1]) if i+1<len(off) else len(ts)
        for j in range(a,b):
            dt=datetime.fromtimestamp(int(ts[j]), timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
            qv=float(vol[j])*float(cl[j])
            batch.append((sym, dt, float(op[j]), float(hi[j]), float(lo[j]), float(cl[j]), float(vol[j]), qv))
            if len(batch)>=5000:
                cur.executemany('insert or replace into price_indicators values (?,?,?,?,?,?,?,?)', batch)
                con.commit(); total+=len(batch); batch.clear()
    if batch:
        cur.executemany('insert or replace into price_indicators values (?,?,?,?,?,?,?,?)', batch)
        con.commit(); total+=len(batch); batch.clear()
    print({'symbols':len(syms),'rows':total,'out_db':args.out_db})
    con.close()

if __name__=='__main__': main()
