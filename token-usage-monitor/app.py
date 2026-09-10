"""
ClusterCloud Token Usage Monitor
--------------------------------
Tiny FastAPI service that aggregates token usage across the ClusterCloud AI
estate and serves a mobile-first dashboard.

Data sources:
  - POST /api/usage and /api/usage/bulk  ingest from any LLM-calling service
  - Ollama watcher (automatic capture)    set OLLAMA_LOG_UNIT (journal) or
                                         OLLAMA_LOG_PATH (file), best-effort
  - Manual logging from the dashboard UI

Run:  uvicorn app:app --host 0.0.0.0 --port 8110
Env:  TUM_DB (sqlite path), TUM_RATES (JSON model rates),
      OLLAMA_LOG_UNIT (systemd unit journal to tail) or OLLAMA_LOG_PATH (file)
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("TUM_DB", str(BASE / "usage.db")))
STATIC_DIR = BASE / "static"

# USD per 1M tokens (input, output), matched by substring, first hit wins.
RATES = {"sonnet": (3.0, 15.0), "opus": (15.0, 75.0), "haiku": (0.8, 4.0),
         "gpt-4o": (2.5, 10.0), "gpt-4": (10.0, 30.0), "default": (1.0, 3.0)}
try:
    RATES = json.loads(os.environ["TUM_RATES"])
except (KeyError, ValueError):
    pass

OLLAMA_LOG = os.environ.get("OLLAMA_LOG_PATH", "").strip()
OLLAMA_UNIT = os.environ.get("OLLAMA_LOG_UNIT", "").strip()

_db = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
_db.row_factory = sqlite3.Row
_lock = threading.RLock()


def q(sql, args=()):
    """SELECT -> list of rows."""
    with _lock:
        return _db.execute(sql, args).fetchall()


def x(sql, args=()):
    """INSERT/UPDATE/DELETE."""
    with _lock:
        _db.execute(sql, args)


def init_db():
    with _lock:
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'unknown',
            model TEXT NOT NULL DEFAULT 'unknown',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL,
            note TEXT);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
    x("INSERT OR IGNORE INTO settings(key,value) VALUES('budget_daily','500000')")
    x("INSERT OR IGNORE INTO settings(key,value) VALUES('budget_alert_pct','80')")


def get_setting(key, default):
    rows = q("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def est_cost(model, tin, tout):
    name = (model or "").lower()
    for k, v in RATES.items():
        if k != "default" and k in name:
            break
    else:
        v = RATES.get("default", (1.0, 3.0))
    return round(v[0] * tin / 1e6 + v[1] * tout / 1e6, 6)


def insert_event(source, model, tin, tout, ts=None, cost=None, note=None):
    ts = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    if cost is None:
        cost = est_cost(model, tin, tout)
    x("INSERT INTO events(ts,source,model,input_tokens,output_tokens,cost_usd,note) "
      "VALUES(?,?,?,?,?,?,?)", (ts, source, model, tin, tout, cost, note))


# ---------------------------------------------------------------- ollama watcher
# Legacy formats (older Ollama): both counts on one line.
_RX_COUNTS = (re.compile(r'"prompt_eval_count"\s*:\s*(\d+).*?"eval_count"\s*:\s*(\d+)'),
              re.compile(r"prompt_eval_count=(\d+).*?\beval_count=(\d+)"))
# Ollama 0.33+ / llama.cpp DEBUG timing (separate lines):
#   prompt eval time = 142.07 ms / 11 tokens
#   eval time = 154.84 ms / 5 tokens
_RX_PROMPT_TOKENS = re.compile(r"prompt eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens")
_RX_EVAL_TOKENS = re.compile(r"(?<!prompt )\beval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens")
_RX_MODEL = (
    re.compile(r'"model"\s*:\s*"([^"]+)"'),
    re.compile(r"\brunner\.name=([^\s,\"']+)"),
    # Avoid matching runner.model=/path/blobs/sha256-...
    re.compile(r"(?<![.\w])model=([^\s,\"'/]+)"),
)


def _short_model(name: str) -> str:
    # registry.ollama.ai/library/qwen3:1.7b → qwen3:1.7b
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("sha256-") or name.startswith("sha256:"):
        return "ollama"
    return name or "ollama"


def parse_ollama(line):
    """Return (input_tokens, output_tokens, model) or None (legacy one-line forms)."""
    for rx in _RX_COUNTS:
        m = rx.search(line)
        if m:
            mm = next((r.search(line) for r in _RX_MODEL if r.search(line)), None)
            model = _short_model(mm.group(1)) if mm else "ollama"
            return int(m.group(1)), int(m.group(2)), model
    return None


def _ollama_lines():
    """Yield Ollama log lines from a systemd unit journal (OLLAMA_LOG_UNIT)
    or a plain log file (OLLAMA_LOG_PATH). File mode re-opens on rotation."""
    while True:
        if OLLAMA_UNIT:
            p = subprocess.Popen(
                ["journalctl", "-u", OLLAMA_UNIT, "-f", "-n", "0", "-o", "cat",
                 "--no-pager"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, errors="replace")
            try:
                for line in p.stdout:
                    yield line
            finally:
                p.kill()
        else:
            try:
                with open(OLLAMA_LOG, "r", errors="replace") as f:
                    f.seek(0, os.SEEK_END)  # tail from end of file
                    while True:
                        line = f.readline()
                        if line:
                            yield line
                            continue
                        try:  # reopen if the log was rotated
                            if os.stat(OLLAMA_LOG).st_ino != os.fstat(f.fileno()).st_ino:
                                break
                        except OSError:
                            pass
                        time.sleep(2)
            except Exception as exc:
                print(f"[token-usage-monitor] ollama watcher: {exc}", file=sys.stderr)
                time.sleep(30)


def _watch_ollama():
    """Stateful watcher: legacy one-line counts, or Ollama 0.33+ split timing lines."""
    while True:
        try:
            current_model = "ollama"
            pending_prompt = None
            for line in _ollama_lines():
                for rx in _RX_MODEL:
                    mm = rx.search(line)
                    if mm:
                        current_model = _short_model(mm.group(1))
                        break

                got = parse_ollama(line)
                if got:
                    insert_event("ollama", got[2], got[0], got[1])
                    pending_prompt = None
                    continue

                mp = _RX_PROMPT_TOKENS.search(line)
                if mp:
                    pending_prompt = int(mp.group(1))
                    continue

                me = _RX_EVAL_TOKENS.search(line)
                if me and pending_prompt is not None:
                    insert_event(
                        "ollama",
                        current_model,
                        pending_prompt,
                        int(me.group(1)),
                    )
                    pending_prompt = None
        except Exception as exc:
            print(f"[token-usage-monitor] ollama watcher: {exc}", file=sys.stderr)
            time.sleep(30)


init_db()
app = FastAPI(title="ClusterCloud Token Usage Monitor")


class UsageEvent(BaseModel):
    source: str = "manual"
    model: str = "unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    ts: Optional[str] = None
    cost_usd: Optional[float] = None
    note: Optional[str] = None


class BudgetSet(BaseModel):
    daily_tokens: int = 500000
    alert_pct: float = 80.0


@app.post("/api/usage")
def add_usage(e: UsageEvent):
    insert_event(e.source, e.model, e.input_tokens, e.output_tokens, e.ts, e.cost_usd, e.note)
    return {"ok": True}


@app.post("/api/usage/bulk")
def add_bulk(events: List[UsageEvent]):
    for e in events:
        insert_event(e.source, e.model, e.input_tokens, e.output_tokens, e.ts, e.cost_usd, e.note)
    return {"ok": True, "inserted": len(events)}


@app.post("/api/budget")
def set_budget(b: BudgetSet):
    x("INSERT INTO settings(key,value) VALUES('budget_daily',?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(b.daily_tokens),))
    x("INSERT INTO settings(key,value) VALUES('budget_alert_pct',?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(b.alert_pct),))
    return {"ok": True, "budget_daily": b.daily_tokens, "alert_pct": b.alert_pct}


@app.get("/api/summary")
def summary(days: int = 7):
    days = max(1, min(days, 90))
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    start = (now - timedelta(days=days - 1)).date().isoformat()
    budget = int(get_setting("budget_daily", "500000"))
    alert_pct = float(get_setting("budget_alert_pct", "80"))
    rows = q("SELECT substr(ts,1,10) AS d, source, model, input_tokens AS it, "
             "output_tokens AS ot, cost_usd FROM events "
             "WHERE substr(ts,1,10) >= ? ORDER BY ts", (start,))
    by_day, by_source, by_model = {}, {}, {}
    tin_w = tout_w = cost_w = 0
    for r in rows:
        d, src, mdl = r["d"], r["source"], r["model"]
        it, ot, cost = r["it"], r["ot"], r["cost_usd"] or 0.0
        tin_w += it
        tout_w += ot
        cost_w += cost
        day = by_day.setdefault(d, {"date": d, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        day["input_tokens"] += it
        day["output_tokens"] += ot
        day["cost_usd"] += cost
        s = by_source.setdefault(src, {"source": src, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        s["input_tokens"] += it
        s["output_tokens"] += ot
        s["cost_usd"] += cost
        m = by_model.setdefault(mdl, {"model": mdl, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        m["input_tokens"] += it
        m["output_tokens"] += ot
        m["cost_usd"] += cost
    days_list = []
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).date().isoformat()
        days_list.append(by_day.get(d, {"date": d, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}))
    t = by_day.get(today, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    used_pct = round((t["input_tokens"] + t["output_tokens"]) * 100.0 / budget, 1) if budget else 0.0
    return {
        "today": {"date": today, "input_tokens": t["input_tokens"],
                  "output_tokens": t["output_tokens"], "cost_usd": round(t["cost_usd"], 4)},
        "budget": {"daily_tokens": budget, "alert_pct": alert_pct, "used_pct": used_pct,
                   "alert": used_pct >= alert_pct},
        "days": days_list,
        "by_source": sorted(by_source.values(), key=lambda v: -(v["input_tokens"] + v["output_tokens"])),
        "by_model": sorted(by_model.values(), key=lambda v: -(v["input_tokens"] + v["output_tokens"])),
        "window": {"days": days, "input_tokens": tin_w, "output_tokens": tout_w,
                   "cost_usd": round(cost_w, 4)},
    }


@app.get("/api/health")
def health():
    mode = ("journal:" + OLLAMA_UNIT) if OLLAMA_UNIT else (OLLAMA_LOG if OLLAMA_LOG else None)
    return {"ok": True, "service": "token-usage-monitor", "db": str(DB_PATH),
            "ollama_watcher": bool(mode), "watcher_mode": mode}


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


if OLLAMA_LOG or OLLAMA_UNIT:
    threading.Thread(target=_watch_ollama, daemon=True, name="ollama-watch").start()
