

# pip install ccxt==4.*
# test_sl.py — відкриває LONG/SHORT БЕЗ базових TP/SL і ставить 2 умовні reduce-only
# LONG:  SL нижче (stop_market)  +  TP вище (take_profit_market)
# SHORT: SL вище (stop_market)   +  TP нижче (take_profit_market)
import os, time
from datetime import datetime, timezone
import ccxt

API_KEY    = os.getenv("BINGX_KEY",    "1zpDoyS5MzGJzcgXeKMx8qp974MKXio4CRXLaYPETssQVah7Zfk9bFLDUtLVhBpFXZkcwbEaFQcBpS9nA")
API_SECRET = os.getenv("BINGX_SECRET", "amCFzf7hgaI8WQmPCVcciwB2yhsYKCdUyR7D5GUWoG2hzIFxfauHora1ADtsl6wjfjCtudvnRujeAcsWJw")
SYMBOL     = os.getenv("SYMBOL", "HIFI/USDT:USDT")
NOTIONAL   = float(os.getenv("NOTIONAL", "4.5"))
LEVERAGE   = int(os.getenv("LEVERAGE", "1"))
POSITION_MODE = os.getenv("POSITION_MODE", "oneway")  # "oneway"|"hedge"
SIDE       = os.getenv("SIDE", "long").lower()        # "long"|"short"
OFFSET_PCT = float(os.getenv("OFFSET_PCT", "0.007"))  # 0.7% від mark

def utcnow(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

def ensure_pos_mode_and_lev(ex, symbol):
    try: ex.set_position_mode(POSITION_MODE == "hedge")
    except Exception as e: print("[warn] set_position_mode:", e)
    try: ex.set_leverage(LEVERAGE, symbol, params={"side":"BOTH"})
    except Exception as e: print("[warn] set_leverage:", e)

def resolve_symbol(ex, symbol):
    """Ensure requested symbol exists and fallback to common alternatives."""
    symbols = getattr(ex, "symbols", []) or []
    if symbol in symbols:
        return symbol

    candidates = []
    if ":" in symbol:
        candidates.append(symbol.split(":")[0])
    if symbol.endswith(":USDT"):
        candidates.append(symbol[:-6])

    for alt in dict.fromkeys(candidates):
        if alt in symbols:
            print(f"[warn] symbol '{symbol}' not found, using '{alt}' instead")
            return alt

    preview = ", ".join(sorted(symbols)[:10])
    raise ValueError(
        f"Exchange does not list symbol '{symbol}'. "
        "Adjust the SYMBOL env var or choose one of the available markets (first 10 shown): "
        f"{preview}"
    )

def amount_for_notional(ex, symbol, last, notional):
    ex.load_markets()
    m = ex.markets[symbol]
    qty = notional / float(last)
    if "precision" in m and m["precision"].get("amount") is not None:
        qty = ex.amount_to_precision(symbol, qty)
    min_qty = float(m.get("limits", {}).get("amount", {}).get("min") or 0)
    if min_qty and float(qty) < min_qty: qty = min_qty
    return float(qty)

def open_pos(ex, symbol, qty, side):
    side_mkt = "buy" if side=="long" else "sell"
    print(f"[{utcnow()}] OPEN {side.upper()} {qty} {symbol}")
    od = ex.create_order(symbol, "market", side_mkt, qty, None,
                         {"reduceOnly": False, "positionSide":"BOTH"})
    print("open_order:", od.get("id"), od.get("info"))
    time.sleep(0.7)
    pos = ex.fetch_positions([symbol])[0]
    entry = float(pos.get("entryPrice") or 0.0)
    mark  = float(pos.get("markPrice") or 0.0)
    qty_p = float(pos.get("contracts") or 0.0)
    print(f"[{utcnow()}] entry={entry} mark={mark} qty={qty_p}")
    return entry, mark, qty_p

def place_conditional(ex, symbol, otype, side, qty, trig, label):
    """otype: 'stop_market' | 'take_profit_market'"""
    base = {"reduceOnly": True, "positionSide":"BOTH", "workingType":"MARK_PRICE"}
    # спроба 1: triggerPrice
    try:
        od = ex.create_order(symbol, otype, side, qty, None, {**base, "triggerPrice": float(trig)})
        print(f"[{utcnow()}] [{label}] via triggerPrice OK id={od.get('id')}")
        return True, str(od.get("id") or od.get("orderId") or "")
    except Exception as e:
        print(f"[{utcnow()}] [{label}] triggerPrice failed -> retry stopPrice: {e}")
        # спроба 2: stopPrice
        od = ex.create_order(symbol, otype, side, qty, None, {**base, "stopPrice": float(trig)})
        print(f"[{utcnow()}] [{label}] via stopPrice OK id={od.get('id')}")
        return True, str(od.get("id") or od.get("orderId") or "")

def main():
    ex = ccxt.bingx({
        "apiKey": API_KEY, "secret": API_SECRET,
        "options": {"defaultType":"swap"},
        "enableRateLimit": True,
    })
    ex.load_markets()
    trading_symbol = resolve_symbol(ex, SYMBOL)
    ensure_pos_mode_and_lev(ex, trading_symbol)

    last = float(ex.fetch_ticker(trading_symbol)["last"])
    qty  = amount_for_notional(ex, trading_symbol, last, NOTIONAL)
    entry, mark, pos_qty = open_pos(ex, trading_symbol, qty, SIDE)

    # робимо «вилку» по обидва боки mark
    above = mark * (1 + OFFSET_PCT)
    below = mark * (1 - OFFSET_PCT)

    # логіка ордерів залежно від напряму позиції
    if SIDE == "long":
        # закриваюча сторона для LONG — sell
        sl_type, sl_trig, sl_side = "stop_market", below, "sell"            # SL нижче
        tp_type, tp_trig, tp_side = "take_profit_market", above, "sell"     # TP вище
    else:  # short
        # закриваюча сторона для SHORT — buy
        sl_type, sl_trig, sl_side = "stop_market", above, "buy"             # SL вище
        tp_type, tp_trig, tp_side = "take_profit_market", below, "buy"      # TP нижче

    qty_each = min(pos_qty, max(0.0, pos_qty * 0.6))
    print(f"[{utcnow()}] placing conditionals: {sl_type}@{sl_trig:.8f} & {tp_type}@{tp_trig:.8f}, qty_each={qty_each}")

    ok1, id1 = place_conditional(ex, trading_symbol, sl_type, sl_side, qty_each, sl_trig, "COND_SL")
    ok2, id2 = place_conditional(ex, trading_symbol, tp_type, tp_side, qty_each, tp_trig, "COND_TP")

    if ok1 and id1:
        print(f"[{utcnow()}] waiting 5s before extending SL distance")
        time.sleep(5)
        # переносимо SL на удвічі більшу відстань
        extended_sl_trig = mark * (1 - 2 * OFFSET_PCT) if SIDE == "long" else mark * (1 + 2 * OFFSET_PCT)
        print(f"[{utcnow()}] extending SL to trigger={extended_sl_trig:.8f}")
        try:
            ex.cancel_order(id1, trading_symbol, params={"positionSide": "BOTH"})
            print(f"[{utcnow()}] cancelled original SL order {id1}")
        except Exception as e:
            print(f"[{utcnow()}] failed to cancel original SL order {id1}: {e}")
        place_conditional(ex, trading_symbol, sl_type, sl_side, qty_each, extended_sl_trig, "COND_SL_EXT")

    time.sleep(0.7)
    oo = ex.fetch_open_orders(trading_symbol) or []
    print(f"[{utcnow()}] open_orders({len(oo)}):")
    for o in oo:
        info = o.get("info") or {}
        print(" -", o.get("id"), info.get("type"), o.get("side"),
              "qty=", o.get("amount"),
              "stop/trigger=", (o.get("triggerPrice") or info.get("stopPrice") or o.get("stopLossPrice") or o.get("takeProfitPrice")),
              "reduceOnly=", info.get("reduceOnly"), "wt=", info.get("workingType"))

    print("\nГотово. У BingX → TPSL/Open Orders мають з’явитися два умовні reduce-only: SL (stop_market) і TP (take_profit_market).")

if __name__ == "__main__":
    main()
