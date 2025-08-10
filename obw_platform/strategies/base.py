
class StrategyBase:
    def __init__(self, cfg: dict): self.cfg = cfg or {}
    def universe(self, t, md_slice): return list(md_slice.keys())
    def rank(self, t, md_slice, symbols): return symbols
    def entry_signal(self, t, sym, row, ctx): return None
    def manage_position(self, t, sym, pos, row, ctx): return {"action":"HOLD","reason":"default"}
