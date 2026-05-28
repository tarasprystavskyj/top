from pathlib import Path
import hashlib

root = Path(__file__).resolve().parent
candidates = list(root.glob('telegram_signals_1m_event_windows_bingx.npz.part*')) + list((root/'npz_parts_go_here').glob('telegram_signals_1m_event_windows_bingx.npz.part*'))
parts = sorted(set(candidates))
if len(parts) != 4:
    raise SystemExit(f"Expected 4 NPZ parts, found {len(parts)}: {[p.name for p in parts]}")
out = root/'local_test_bundle/DB/telegram_signals_1m_event_windows_bingx.npz'
out.parent.mkdir(parents=True, exist_ok=True)
h = hashlib.sha256()
with out.open('wb') as f:
    for p in parts:
        data = p.read_bytes()
        h.update(data)
        f.write(data)
print(f"Wrote: {out}")
print(f"Size: {out.stat().st_size:,} bytes")
print(f"sha256: {h.hexdigest()}")
