
#!/usr/bin/env python3
# preflight_bingx_active_universe.py
# Reads a universe text file and writes a filtered version that only contains
# ACTIVE BingX USDT-margined linear swap markets (quote=USDT, contract=True, active=True).
#
# Usage:
#   python3 preflight_bingx_active_universe.py --in uuniverse_v5_avaai_5m_5000.txt --out universe_active.txt
#
import argparse, sys
from pathlib import Path

def load_lines(p):
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="outp", required=True)
    ap.add_argument("--exchange", default="bingx")
    args = ap.parse_args()

    try:
        import ccxt
    except Exception as e:
        print("[ERR] ccxt not installed:", e, file=sys.stderr); sys.exit(2)

    ex = getattr(ccxt, args.exchange)()
    ex.load_markets()

    # Build active USDT-m linear swap set in CCXT's unified symbol format (e.g., 'MKR/USDT:USDT')
    active = set()
    for m in ex.markets.values():
        if not m.get("active", False):
            continue
        if m.get("linear") is not True:
            continue
        if m.get("quote") != "USDT":
            continue
        t = m.get("type") or m.get("contractType")
        if t not in ("swap", "future"):  # bingx uses 'swap'
            continue
        sym = m.get("symbol")
        if sym:
            active.add(sym)

    inp_syms = load_lines(args.inp)
    # Match by stripping ':USDT' suffix if present, then try both variants
    def variants(s):
        base = s.split(":")[0]
        return [s, base]

    kept, dropped = [], []
    for s in inp_syms:
        keep = False
        for v in variants(s):
            if v in active:
                keep = True; break
        (kept if keep else dropped).append(s)

    Path(args.outp).write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"[preflight] input={len(inp_syms)} kept={len(kept)} dropped={len(dropped)} -> {args.outp}")
    if dropped:
        Path(args.outp + ".dropped.txt").write_text("\n".join(dropped) + "\n", encoding="utf-8")
        print(f"[preflight] wrote dropped list -> {args.outp}.dropped.txt")

if __name__ == "__main__":
    main()
