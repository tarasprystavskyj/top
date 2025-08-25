# runners/universe_filter.py
from __future__ import annotations
import os

def _read_lines(path: str):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    out.append(ln)
    except Exception:
        pass
    return out

def load_universe(cfg: dict, args=None):
    """Return (allow_set, deny_set) from cfg['universe'] / args.{universe_file,allow_symbols,deny_symbols}."""
    uni = (cfg or {}).get("universe", {}) or {}
    allow = set(uni.get("allow", []) or [])
    deny  = set(uni.get("deny",  []) or [])

    # YAML root keys for backward compatibility
    if "universe_file" in (cfg or {}) and "file" not in uni:
        uni["file"] = cfg.get("universe_file")

    # CLI args (if runner forwarded them)
    if args is not None:
        if getattr(args, "universe_file", ""):
            uni["file"] = getattr(args, "universe_file")
        for key, dst in [("allow_symbols", allow), ("deny_symbols", deny)]:
            val = getattr(args, key, "")
            if val:
                for sym in str(val).split(","):
                    sym = sym.strip()
                    if sym:
                        dst.add(sym)

    if uni.get("file"):
        for sym in _read_lines(uni["file"]):
            allow.add(sym)

    return allow, deny

def filter_md(md_slice: dict, allow: set, deny: set):
    """Filter a {symbol: row} slice using allow/deny sets. If allow empty -> only apply deny."""
    if not md_slice:
        return md_slice
    if not allow and not deny:
        return md_slice
    out = {}
    for k, v in md_slice.items():
        if allow and k not in allow:
            continue
        if k in deny:
            continue
        out[k] = v
    return out
