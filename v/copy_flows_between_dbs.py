
#!/usr/bin/env python3
# copy_flows_between_dbs.py
# Копіює таблицю btc_pair_flows з DB1 у DB2 (перезаписує).
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con_src = sqlite3.connect(src)
con_dst = sqlite3.connect(dst)
cur_s = con_src.cursor()
cur_d = con_dst.cursor()
cur_d.execute("""CREATE TABLE IF NOT EXISTS btc_pair_flows (
    datetime_utc TEXT NOT NULL,
    base TEXT NOT NULL,
    alt  TEXT NOT NULL,
    pair_symbol TEXT NOT NULL,
    gross_btc REAL NOT NULL,
    btc_usd REAL NOT NULL,
    gross_usd REAL NOT NULL,
    ret REAL NOT NULL,
    dir INTEGER NOT NULL,
    net_flow_usd REAL NOT NULL,
    thickness REAL,
    PRIMARY KEY(datetime_utc, pair_symbol)
)""")
cur_d.execute("DELETE FROM btc_pair_flows")
for row in cur_s.execute("SELECT datetime_utc, base, alt, pair_symbol, gross_btc, btc_usd, gross_usd, ret, dir, net_flow_usd, thickness FROM btc_pair_flows"):
    cur_d.execute("INSERT OR REPLACE INTO btc_pair_flows VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
con_dst.commit()
print("Copied btc_pair_flows from", src, "to", dst)
