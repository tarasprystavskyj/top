#!/usr/bin/env python3
import argparse, os, json, glob
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--symbol', default='FREEDOMMONEY')
    args = ap.parse_args()
    root = Path(args.root)
    sym = args.symbol.lower().replace('/', '').replace(':', '').replace('-', '').replace('_', '')
    patterns = ['*.npz','*.db','*.sqlite','*.csv','*.txt']
    hits=[]
    for pat in patterns:
        for p in root.rglob(pat):
            sp = str(p).lower().replace('/', '').replace(':', '').replace('-', '').replace('_', '')
            if sym in sp or 'freedom' in sp:
                try: size=p.stat().st_size
                except Exception: size=0
                hits.append((size, str(p)))
    hits.sort(reverse=True)
    print('FREEDOMMONEY local data candidates:')
    for size,p in hits[:200]:
        print(f'{size:12d} {p}')
    print('\nUniverse files mentioning FREEDOM/FREEDOMMONEY:')
    for p in root.rglob('universe*'):
        if p.is_file():
            try:
                txt=p.read_text(errors='ignore')[:200000]
            except Exception:
                continue
            if 'FREEDOM' in txt.upper():
                print(p)
                for line in txt.splitlines():
                    if 'FREEDOM' in line.upper(): print('  ', line)
