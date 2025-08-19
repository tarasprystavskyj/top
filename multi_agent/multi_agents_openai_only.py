#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, os, sys, json, time, csv, math, hashlib, sqlite3, shutil, subprocess, re
from datetime import datetime
from pathlib import Path

try:
    import yaml
except Exception:
    print("Please: pip install pyyaml", file=sys.stderr); raise

# ────────────────────────────────────────────────────────────────────────────────
# ANSI colors
C_RESET="\033[0m"; C_DIM="\033[2m"; C_BOLD="\033[1m"
C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_BLUE="\033[34m"; C_MAGENTA="\033[35m"; C_CYAN="\033[36m"; C_RED="\033[31m"

def cfmt(s, c): return f"{c}{s}{C_RESET}"

def tee_start(log_file):
    if not log_file: return None
    try:
        lf = open(log_file, "a", buffering=1, encoding="utf-8")
        lf.write(f"{C_DIM}[log]{C_RESET} tee -> {log_file}\n")
        return lf
    except Exception as e:
        print(cfmt(f"[warn] cannot open log file: {e}", C_YELLOW))
        return None

def tprint(msg, lf=None):
    print(msg)
    if lf: 
        # прибираємо ANSI для логу
        lf.write(re.sub(r"\x1b\\[[0-9;]*m","",msg)+("\n" if not msg.endswith("\n") else ""))

# ────────────────────────────────────────────────────────────────────────────────
# OpenAI (критик) — легкий клієнт з підтримкою різних полів max_* і temp
def _model_caps(model:str):
    # Спрощена матриця сумісності (на основі помилок з логів)
    if model in ("gpt-5-mini","gpt-5-nano"):
        return dict(max_field="max_completion_tokens", allow_temperature=False)
    if model in ("gpt-4o-mini",):
        return dict(max_field="max_tokens", allow_temperature=True)
    # Fallback
    return dict(max_field="max_tokens", allow_temperature=True)

def _openai_chat(api_key, model, messages, max_tokens=None, temperature=None):
    import requests
    url="https://api.openai.com/v1/chat/completions"
    caps=_model_caps(model)
    payload={"model":model,"messages":messages}
    if max_tokens:
        payload[caps["max_field"]]=int(max_tokens)
    if temperature is not None and caps["allow_temperature"]:
        payload["temperature"]=float(temperature)
    headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"}
    r=requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    if r.status_code!=200:
        raise RuntimeError(f"Error code: {r.status_code} - {r.text}")
    data=r.json()
    txt=data["choices"][0]["message"]["content"]
    # дешева оцінка вартості: беремо usage, якщо є
    usage=data.get("usage",{})
    in_t=usage.get("prompt_tokens",0); out_t=usage.get("completion_tokens",0)
    return txt, in_t, out_t

# ────────────────────────────────────────────────────────────────────────────────
# файли/ямули/хеші
def sha256_path(p:Path)->str:
    with open(p,"rb") as f: return hashlib.sha256(f.read()).hexdigest()

def read_yaml(p:Path)->dict:
    with open(p,"r",encoding="utf-8") as f: return yaml.safe_load(f)

def write_yaml(p:Path, data:dict):
    tmp=p.with_suffix(p.suffix+".tmp")
    with open(tmp,"w",encoding="utf-8") as f: yaml.safe_dump(data,f,sort_keys=False,allow_unicode=True)
    tmp.replace(p)

def get_nested(d, dotted, default=None):
    cur=d
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur: return default
        cur=cur[k]
    return cur

def set_nested(d, dotted, value):
    parts=dotted.split(".")
    cur=d
    for k in parts[:-1]:
        if k not in cur or not isinstance(cur[k],dict): cur[k]={}
        cur=cur[k]
    cur[parts[-1]]=value

# ────────────────────────────────────────────────────────────────────────────────
# CSV summary parser з підтримкою inf/NaN
def _to_float(x):
    if x is None: return None
    s=str(x).strip()
    if s.lower() in ("inf","+inf"): return float("inf")
    if s.lower() in ("-inf"): return float("-inf")
    if s.lower() in ("nan","-nan"): return float("nan")
    try: return float(s)
    except: return None

def parse_summary(summary_csv:Path):
    # очікуємо колонок: equity_end, trades, profit_factor, max_dd, win_rate
    # підставимо fallback для назв з різними регістрами/пробілами
    if not summary_csv.exists():
        raise FileNotFoundError(str(summary_csv))
    with open(summary_csv,"r",encoding="utf-8") as f:
        rdr=csv.DictReader(f)
        row=None
        for row in rdr: pass
    if not row: raise RuntimeError("summary.csv is empty")
    def pick(*names, default=None):
        for n in names:
            for cand in (n, n.lower(), n.replace(" ","_"), n.replace(" ","").lower()):
                if cand in row: return row[cand]
        return default
    return {
        "equity_end": _to_float(pick("equity_end","Equity end")),
        "trades": _to_float(pick("trades","Trades")),
        "profit_factor": _to_float(pick("profit_factor","Profit Factor","profitfactor")),
        "max_dd": _to_float(pick("max_dd","Max DD","max_drawdown")),
        "win_rate": _to_float(pick("win_rate","Win-rate","winrate","win_rate_percent"))
    }

# рендер метрик
def fmt_metrics(m):
    def f(k):
        v=m.get(k)
        if v is None: return "—"
        if isinstance(v,float) and (math.isinf(v) or math.isnan(v)): return str(v)
        if k in ("win_rate","profit_factor","max_dd"): 
            # dd/wr/pf: 2-4 знаки
            if k=="win_rate": return f"{v:.2f}"
            if k=="max_dd": return f"{v:.4f}"
            return f"{v:.6f}"
        return f"{v:.10f}".rstrip("0").rstrip(".")
    return (
        f"Equity end: {f('equity_end')}\n"
        f"Trades: {f('trades')}\n"
        f"Profit Factor: {f('profit_factor')}\n"
        f"Max DD: {f('max_dd')}\n"
        f"Win-rate: {f('win_rate')}"
    )

def annualize(period_days, start_equity, end_equity):
    if not start_equity or start_equity<=0: return (0,0,0)
    R=end_equity/start_equity
    period_pct=(R-1.0)*100.0
    if period_days and period_days>0:
        monthly=(R**(30.0/period_days)-1.0)*100.0
        yearly=(R**(365.0/period_days)-1.0)*100.0
    else:
        monthly=yearly=float("nan")
    return period_pct, monthly, yearly

# ────────────────────────────────────────────────────────────────────────────────
# SQLite для накопичення найкращих результатів
def db_init(db_path:Path):
    conn=sqlite3.connect(db_path)
    c=conn.cursor()
    c.execute("""create table if not exists runs(
        id integer primary key,
        ts text,
        cfg_sha text,
        cfg_path text,
        param_name text,
        param_value text,
        equity_end real,
        trades real,
        profit_factor real,
        max_dd real,
        win_rate real,
        equity_start real,
        period_days real
    )""")
    c.execute("""create table if not exists kv(
        k text primary key,
        v text
    )""")
    conn.commit()
    return conn

def db_set(conn, k, v):
    conn.execute("insert into kv(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))
    conn.commit()

def db_get(conn, k, default=None):
    cur=conn.execute("select v from kv where k=?",(k,))
    row=cur.fetchone()
    if not row: return default
    try: return json.loads(row[0])
    except: return default

def db_best_equity(conn):
    cur=conn.execute("select equity_end, cfg_path, cfg_sha from runs order by equity_end desc limit 1")
    r=cur.fetchone()
    if not r: return None
    return {"equity_end": r[0], "cfg_path": r[1], "cfg_sha": r[2]}

def db_insert_run(conn, row):
    conn.execute("""insert into runs 
        (ts,cfg_sha,cfg_path,param_name,param_value,equity_end,trades,profit_factor,max_dd,win_rate,equity_start,period_days)
        values(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.utcnow().isoformat(timespec="seconds"),
         row["cfg_sha"], row["cfg_path"], row.get("param_name"), str(row.get("param_value")),
         row["equity_end"], row["trades"], row["profit_factor"], row["max_dd"], row["win_rate"],
         row.get("equity_start"), row.get("period_days")))
    conn.commit()

# ────────────────────────────────────────────────────────────────────────────────
# Критик (порівняння з попередньою ітерацією)
def critic_suggest(api_key, model, prev_metrics, new_metrics, max_tokens, uah_rate):
    # якщо жодних змін — повертати None (і не рахувати cost)
    if prev_metrics == new_metrics: 
        return None, 0, 0, 0.0
    prompt = (
        "Ти критик торгової стратегії. Отримай дві метрики і поверни JSON з полями:\n"
        '{"next_param":"<ім\'я параметра>","direction":"up|down","pct":0.05..0.20,"note":"коротка порада"}\n'
        f"Попередні: {prev_metrics}\n"
        f"Нові: {new_metrics}\n"
        "Підкажи один параметр з already_changed або повʼязані (atr, momentum, qv...).\n"
        "Якщо стало гірше — direction протилежний останньому кроку, pct ≈ 0.1.\n"
    )
    msgs=[{"role":"user","content":prompt}]
    try:
        txt, tin, tout = _openai_chat(api_key, model, msgs, max_tokens=max_tokens, temperature=None)
        cost_usd = est_cost_usd(model, tin, tout)
        return txt.strip(), tin, tout, cost_usd
    except Exception as e:
        return f"[critic warn] {e}", 0, 0, 0.0

# дуже груба оцінка вартості (можеш підкоригувати коефіцієнти при потребі)
def est_cost_usd(model, in_t, out_t):
    # умовні тарифи
    prices = {
        "gpt-5-mini": (0.0005/1000, 0.0015/1000),
        "gpt-5-nano": (0.0002/1000, 0.0006/1000),
        "gpt-4o-mini": (0.0005/1000, 0.0015/1000),
    }
    pin, pout = prices.get(model, (0.0005/1000, 0.0015/1000))
    return in_t*pin + out_t*pout

# ────────────────────────────────────────────────────────────────────────────────
# Запуск бектесту
def run_backtest(project_dir:Path, cfg_relpath:str, logf):
    cmd=["python", str(project_dir/"backtester_core.py"), "--cfg", cfg_relpath]
    tprint(cfmt(C_BOLD+"▶ Run:"+C_RESET+" "+ " ".join(cmd), C_CYAN), logf)
    p=subprocess.run(cmd, cwd=str(project_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out=p.stdout.strip()
    tprint(cfmt("[summary] оновлено", C_DIM), logf)
    tprint(cfmt("Backtest finished with code="+str(p.returncode), C_GREEN if p.returncode==0 else C_RED), logf)
    return p.returncode, out

# ────────────────────────────────────────────────────────────────────────────────
# Параметричне налаштування
TUNE_PARAMS_ORDER = [
    "strategy_params.min_momentum_sum",
    "strategy_params.mom_flip_thresh",
    "strategy_params.min_atr_ratio",
    "strategy_params.min_qv_1h",
    "strategy_params.min_qv_24h",
    "strategy_params.trail_start_atr",
    "strategy_params.trail_dist_atr",
    # "limit_bars"  # НЕ чіпаємо
]

def step_value(v, direction, pct):
    try:
        v=float(v)
        if direction=="up": return v*(1.0+pct)
        else: return v*(1.0-pct)
    except:
        # для цілих
        try:
            vi=int(v)
            delta=max(1,int(round(vi*pct)))
            return vi+delta if direction=="up" else max(0,vi-delta)
        except:
            return v

# ────────────────────────────────────────────────────────────────────────────────
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--task-file", default=None)
    ap.add_argument("--code-zip", default=None)  # залишаємо для сумісності
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--prerun", action="store_true")
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--tuned-config-name", default="cs_C2_tuned_1h.yaml")
    ap.add_argument("--max-tokens", type=int, default=800)
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--uah-rate", type=float, default=float(os.environ.get("UAH_RATE", "41")))

    ap.add_argument("--model", default="gpt-5-mini", choices=["gpt-5-mini","gpt-5-nano","gpt-4o-mini"])
    ap.add_argument("--critic-model", default="gpt-5-mini", choices=["gpt-5-mini","gpt-5-nano","gpt-4o-mini"])
    ap.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY",""))
    ap.add_argument("--period-days", type=float, default=None, help="override")
    ap.add_argument("--equity-start", type=float, default=None, help="override")
    ap.add_argument("--debug", action="store_true")

    args=ap.parse_args()
    project_dir=Path(args.project_dir).resolve()
    logf=tee_start(args.log_file)

    tprint(cfmt("Args:", C_DIM)+f" project_dir={project_dir} | cycles={args.cycles} | tuned={args.tuned-config-name if hasattr(args,'tuned-config-name') else args.tuned_config_name} | model={args.model} | critic=True({args.critic_model}) | period_days={args.period_days} | equity_start={args.equity_start}", logf)

    # PRERUN: встановлення залежностей один раз (кешуємо простим файлом-міткою)
    cache_flag=project_dir/".prerun_ok"
    if args.prerun and not cache_flag.exists():
        tprint(cfmt("[PRERUN cache] MISS → installing deps…", C_DIM), logf)
        # тут можна поставити ваші pip install при потребі
        cache_flag.write_text("ok")
        tprint(cfmt("[PRERUN] OK", C_GREEN), logf)
    else:
        tprint(cfmt("[PRERUN cache] HIT", C_DIM), logf)
        tprint(cfmt("[PRERUN] OK", C_GREEN), logf)

    base_cfg_rel="configs/cs_C2_base_1h.yaml"
    tuned_cfg_rel=args.tuned_config_name
    base_cfg=project_dir/base_cfg_rel
    tuned_cfg=project_dir/tuned_cfg_rel

    # створюємо tuned, якщо нема
    if not tuned_cfg.exists():
        shutil.copy2(base_cfg, tuned_cfg)

    # auto period_days/equity_start з YAML, який реально запускаємо на baseline
    base_yaml=read_yaml(base_cfg)
    tuned_yaml=read_yaml(tuned_cfg)

    def read_initial_equity(cfg_yaml, fallback=100.0):
        v=get_nested(cfg_yaml,"initial_equity",None)
        try: return float(v) if v is not None else fallback
        except: return fallback

    # якщо користувач не задав override — беремо з base YAML
    equity_start = args.equity_start if args.equity_start is not None else read_initial_equity(base_yaml, 100.0)
    if args.period_days is None:
        # спробуємо вгадати по вашому кешу: тут просто хардкод 60, бо ви так і просили
        period_days = 60.0
        src="default/guess"
    else:
        period_days = float(args.period_days); src="CLI"

    tprint(cfmt("[auto]", C_DIM)+f" equity_start={equity_start} (src=yaml)", logf)
    tprint(cfmt("[auto]", C_DIM)+f" period_days={period_days} (src={src})", logf)

    # summary.csv фіксований
    summary_csv = project_dir/"summary.csv"

    # DB
    db_path=project_dir/"autogen_results.sqlite"
    conn=db_init(db_path)

    # baseline (SESSION)
    tprint(cfmt(C_BOLD+"=== BASELINE RUN (SESSION) ==="+C_RESET, C_BLUE), logf)
    # базовий YAML та initial_equity беремо з base
    rc,_=run_backtest(project_dir, base_cfg_rel, logf)
    sm=parse_summary(summary_csv)

    tprint(cfmt("[summary] path: "+str(summary_csv), C_DIM), logf)
    tprint(fmt_metrics(sm), logf)

    # розрахунок доходності
    period_pct, monthly, yearly = annualize(period_days, equity_start, sm["equity_end"])
    tprint(cfmt("Baseline returns:", C_MAGENTA)+f" Period={period_pct:+.2f}%  Monthly={monthly:+.2f}%  Annual={yearly:+.2f}%", logf)

    # best у БД
    best_db=db_best_equity(conn)
    session_best=sm["equity_end"]
    session_best_cfg=base_cfg_rel

    # запис у БД
    db_insert_run(conn, {
        "cfg_sha": sha256_path(base_cfg),
        "cfg_path": str(base_cfg_rel),
        "param_name": None,
        "param_value": None,
        "equity_end": sm["equity_end"], "trades": sm["trades"], "profit_factor": sm["profit_factor"],
        "max_dd": sm["max_dd"], "win_rate": sm["win_rate"],
        "equity_start": equity_start, "period_days": period_days,
    })

    # показ з чим порівнюємо глобально
    if best_db:
        tprint(cfmt("[resume]", C_DIM)+ " DB best exists — using global best for end-of-run comparison.", logf)

    # ітеративний тюнінг
    already = set()
    prev_metrics = sm
    stuck=0
    total_cost_usd=0.0

    # набір параметрів для тюнінгу (не чіпаємо limit_bars)
    tune_list = [p for p in TUNE_PARAMS_ORDER if "limit_bars" not in p]

    for i in range(1, args.cycles+1):
        tprint(cfmt(C_BOLD+f"=== TUNE ITERATION #{i} ==="+C_RESET, C_BLUE), logf)
        # вибір параметра
        # простий вибір: перший, що ще не міняли; якщо всі міняли — по колу
        pick = None
        for p in tune_list:
            if p not in already:
                pick=p; break
        if not pick: pick=tune_list[(i-1)%len(tune_list)]
        already.add(pick)

        # напрямок: якщо є підказка критика в попередньому кроці — використати її, інакше heuristics
        direction = "up"
        pct = 0.10
        # heuristics: якщо equity_end погіршився на попередньому кроці — рух у протилежний бік
        # тут беремо попереднє порівняння вже включене нижче; для першої ітерації — за замовчуванням

        # поточне значення у tuned_yaml (візьмемо з tuned, бо саме його будемо змінювати)
        tuned_yaml = read_yaml(tuned_cfg)
        cur_val = get_nested(tuned_yaml, pick, get_nested(base_yaml, pick, None))
        new_val = step_value(cur_val, direction, pct)

        # запис у tuned
        set_nested(tuned_yaml, pick, new_val)
        write_yaml(tuned_cfg, tuned_yaml)

        # DEBUG: перевіримо, що реально записали
        check_yaml = read_yaml(tuned_cfg)
        check_val = get_nested(check_yaml, pick, None)
        tprint(cfmt("[debug] persisted "+pick+f" = {check_val} (from {tuned_cfg})", C_CYAN), logf)

        # SHA для наочності
        try:
            tprint(cfmt("[debug] tuned SHA: "+sha256_path(tuned_cfg), C_DIM), logf)
        except: pass

        tprint(cfmt("Змінив параметр:", C_MAGENTA)+f" {pick}: {cur_val} → "+cfmt(str(check_val), C_BOLD), logf)
        tprint(cfmt("Новий файл:", C_DIM)+f" {tuned_cfg}", logf)

        # запускаємо з ТЮНЕННИМ cfg
        tprint(cfmt("[debug] using cfg: "+tuned_cfg_rel, C_CYAN), logf)
        rc,_=run_backtest(project_dir, tuned_cfg_rel, logf)
        sm2=parse_summary(summary_csv)
        tprint(fmt_metrics(sm2), logf)

        # returns
        eq0 = read_initial_equity(read_yaml(project_dir/tuned_cfg_rel), equity_start)
        p2, m2, y2 = annualize(period_days, eq0, sm2["equity_end"])
        tprint(cfmt("Tuned returns:", C_MAGENTA)+f" Period={p2:+.2f}%  Monthly={m2:+.2f}%  Annual={y2:+.2f}%", logf)

        # вставка у БД
        db_insert_run(conn, {
            "cfg_sha": sha256_path(project_dir/tuned_cfg_rel),
            "cfg_path": str(tuned_cfg_rel),
            "param_name": pick,
            "param_value": check_val,
            "equity_end": sm2["equity_end"], "trades": sm2["trades"], "profit_factor": sm2["profit_factor"],
            "max_dd": sm2["max_dd"], "win_rate": sm2["win_rate"],
            "equity_start": eq0, "period_days": period_days,
        })

        # локальний best-of-run (за equity_end)
        if sm2["equity_end"]>session_best:
            session_best=sm2["equity_end"]; session_best_cfg=tuned_cfg_rel
            tprint(cfmt("[BEST] Новий найкращий у сесії!", C_YELLOW), logf)

        # КРИТИК: порівняння з попередньою ітерацією
        diffs = (sm2["equity_end"]!=prev_metrics["equity_end"] or
                 sm2["trades"]!=prev_metrics["trades"] or
                 sm2["profit_factor"]!=prev_metrics["profit_factor"] or
                 sm2["max_dd"]!=prev_metrics["max_dd"] or
                 sm2["win_rate"]!=prev_metrics["win_rate"])
        if args.openai_api_key and diffs:
            txt, tin, tout, usd = critic_suggest(args.openai_api_key, args.critic_model, prev_metrics, sm2, args.max_tokens, args.uah_rate)
            total_cost_usd += usd
            if txt and not txt.startswith("[critic warn]"):
                tprint(cfmt("CRITIC SAYS: ", C_BLUE)+txt, logf)
            else:
                tprint(cfmt(str(txt), C_YELLOW), logf)
        else:
            if not diffs:
                stuck+=1
                tprint(cfmt("[stuck] метрики не змінились", C_YELLOW)+f" (#{stuck}) — збільшу крок/або зміню параметр далі", logf)

        prev_metrics=sm2

    # Кінець сесії — порівняння з глобальним best у БД
    best_db_now=db_best_equity(conn)
    if best_db_now and session_best>best_db_now["equity_end"]:
        # Новий глобальний best — зберігаємо improved
        src = project_dir/session_best_cfg
        dst = project_dir/"configs/cs_C2_improved_1h.yaml"
        shutil.copy2(src, dst)
        tprint(cfmt(C_BOLD+"=== BEST OF RUN (by equity_end) — SAVED ==="+C_RESET, C_GREEN), logf)
        tprint(f"Config: {session_best_cfg}  →  saved as configs/cs_C2_improved_1h.yaml", logf)
    else:
        tprint(cfmt("[note] За сесію глобально кращого за БД не знайдено.", C_YELLOW), logf)

    tprint(cfmt("--- BEST METRICS (current session) ---", C_BLUE), logf)
    tprint(f"CFG: {session_best_cfg}", logf)
    # підвантажимо відповідний summary (ми зберігали останній; для простоти показуємо останні метрики prev_metrics)
    tprint(fmt_metrics(prev_metrics), logf)

    if total_cost_usd:
        tprint(cfmt("=== COST SUMMARY ===", C_DIM), logf)
        tprint(f"total_est=${total_cost_usd:.6f}  (~₴{total_cost_usd*args.uah_rate:.2f} @ {args.uah_rate} UAH/USD)", logf)
    else:
        tprint(cfmt("=== COST SUMMARY ===", C_DIM), logf)
        tprint("total_est=$0.000000", logf)

    if logf: logf.close()

if __name__=="__main__":
    main()
