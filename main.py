# main.py
"""
FastAPI leaderboard backend for Kenzies Fridge.

Usage:
    pip install fastapi uvicorn
    python -m uvicorn main:app --reload

Serves:
 - GET  /api/leaderboard?limit=100
 - GET  /api/user/{user_id}
 - POST /api/user/{user_id}        body: {"nickname": "...", "balance": 123.45, "trades":0, "wins":0, "period_start_balance":5000}
 - POST /api/user/{user_id}/trade body: {"result":"win"|"lose", "amount": 12.34}  (records a trade)
 - POST /api/close_month
 - GET  /api/winners/{month}
 - GET  /api/winners
Additionally serves static files from ./static/
Data stored in ./data/leaderboard.json
"""
import os
import json
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# default starting balance for new users
START_BALANCE = 5000.0

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "leaderboard.json")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)

_lock = threading.Lock()

def _now_iso():
    return datetime.utcnow().isoformat() + "Z"

def _read_db() -> Dict[str, Any]:
    if not os.path.exists(DB_PATH):
        default = {
            "users": {},  # userId -> { nickname, balance, last_update, trades, wins, period_start_balance }
            "monthly_winners": {},  # "YYYY-MM" -> { "podium": [...], "closed_at": ISO }
            "last_month_closed": None
        }
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_db(data: Dict[str, Any]):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _get_month_key(dt: Optional[datetime]=None) -> str:
    dt = dt or datetime.utcnow()
    return dt.strftime("%Y-%m")

def _prev_month_key(dt: Optional[datetime]=None) -> str:
    dt = dt or datetime.utcnow()
    first = dt.replace(day=1)
    prev_last = first - timedelta(days=1)
    return prev_last.strftime("%Y-%m")

def compute_podium_snapshot(users: Dict[str, Any], top_n=3):
    # users: userId -> {nickname, balance, last_update}
    arr = []
    for uid, u in users.items():
        try:
            bal = float(u.get("balance", START_BALANCE))
        except Exception:
            bal = START_BALANCE
        arr.append((uid, u.get("nickname", ""), bal))
    arr.sort(key=lambda x: x[2], reverse=True)
    podium = []
    for i in range(min(top_n, len(arr))):
        uid, nick, bal = arr[i]
        podium.append({
            "position": i+1,
            "user_id": uid,
            "nickname": nick,
            "balance": round(bal, 2)
        })
    return podium

def compute_user_metrics(user_record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns a dict with:
      - performance: percent change from period_start_balance to balance (float)
      - win_rate: percent wins/trades (float)
      - trades_this_period: int
    """
    try:
        balance = float(user_record.get("balance", START_BALANCE))
    except Exception:
        balance = START_BALANCE
    try:
        start = float(user_record.get("period_start_balance", START_BALANCE))
    except Exception:
        start = START_BALANCE

    # Avoid division by zero
    if start == 0:
        performance = 0.0
    else:
        performance = ((balance - start) / start) * 100.0

    trades = int(user_record.get("trades", 0) or 0)
    wins = int(user_record.get("wins", 0) or 0)
    if trades <= 0:
        win_rate = 0.0
    else:
        win_rate = (wins / trades) * 100.0

    return {
        "performance": round(performance, 2),
        "win_rate": round(win_rate, 2),
        "trades_this_period": trades,
        "wins": wins,
        "period_start_balance": round(start, 2),
        "balance": round(balance, 2)
    }

# Pydantic models
class UserUpdate(BaseModel):
    nickname: Optional[str] = None
    balance: Optional[float] = None
    # optional metrics that can be set directly
    trades: Optional[int] = None
    wins: Optional[int] = None
    period_start_balance: Optional[float] = None

class TradeRecord(BaseModel):
    result: str = Field(..., description='Either "win" or "lose"')
    amount: Optional[float] = None  # amount to add/subtract from balance (optional)

app = FastAPI(title="Kenzies Fridge Leaderboard API")

# Allow local dev from browser if served elsewhere
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Important: mount static at /static to avoid shadowing /api routes.
# Serve index.html explicitly at the root.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=FileResponse)
def root_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    # fallback: minimal JSON response if no index.html present
    return JSONResponse({"message": "Kenzies Fridge API - static index not found"}, status_code=200)

@app.get("/api/leaderboard")
def get_leaderboard(limit: int = 100):
    """Return top `limit` users sorted by balance desc with extra metrics."""
    with _lock:
        db = _read_db()
    users = db.get("users", {})
    arr = []
    for uid, u in users.items():
        metrics = compute_user_metrics(u)
        arr.append({
            "user_id": uid,
            "nickname": u.get("nickname", ""),
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"]
        })
    arr.sort(key=lambda x: x["balance"], reverse=True)
    return {"leaderboard": arr[:max(0, min(limit, 1000))], "timestamp": _now_iso()}

@app.get("/api/user/{user_id}")
def get_user(user_id: str):
    with _lock:
        db = _read_db()
    user = db.get("users", {}).get(user_id)
    if user:
        metrics = compute_user_metrics(user)
        # merge user record with computed metrics for convenience
        return {
            "user_id": user_id,
            "nickname": user.get("nickname", ""),
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"],
            "wins": metrics["wins"],
            "period_start_balance": metrics["period_start_balance"],
            "last_update": user.get("last_update")
        }
    else:
        # return default empty user with START_BALANCE and zeroed metrics
        return {
            "user_id": user_id,
            "nickname": "",
            "balance": round(START_BALANCE, 2),
            "performance": 0.0,
            "win_rate": 0.0,
            "trades_this_period": 0,
            "wins": 0,
            "period_start_balance": round(START_BALANCE, 2),
            "last_update": None
        }

@app.post("/api/user/{user_id}")
def post_user(user_id: str, upd: UserUpdate):
    """
    Create or update a user record. Accepts optional trades/wins/period_start_balance.
    Also automatically snapshots previous month if rollover detected.
    """
    with _lock:
        db = _read_db()
        # check month rollover and close previous month if needed
        current_month = _get_month_key()
        last_closed = db.get("last_month_closed")
        if last_closed != current_month:
            prev_month = _prev_month_key()
            if prev_month not in db.get("monthly_winners", {}):
                podium = compute_podium_snapshot(db.get("users", {}), top_n=3)
                if podium:
                    db.setdefault("monthly_winners", {})[prev_month] = {"podium": podium, "closed_at": _now_iso()}
            db["last_month_closed"] = current_month

        users = db.setdefault("users", {})
        # create new user with START_BALANCE and zeroed metrics if missing
        u = users.setdefault(user_id, {
            "nickname": "",
            "balance": START_BALANCE,
            "last_update": None,
            "trades": 0,
            "wins": 0,
            "period_start_balance": START_BALANCE
        })
        if upd.nickname is not None:
            u["nickname"] = upd.nickname[:40]
        if upd.balance is not None:
            try:
                u["balance"] = round(float(upd.balance), 2)
            except Exception:
                u["balance"] = START_BALANCE
        # optional metrics updates
        if upd.trades is not None:
            try:
                u["trades"] = int(upd.trades)
            except Exception:
                u["trades"] = 0
        if upd.wins is not None:
            try:
                u["wins"] = int(upd.wins)
            except Exception:
                u["wins"] = 0
        if upd.period_start_balance is not None:
            try:
                u["period_start_balance"] = round(float(upd.period_start_balance), 2)
            except Exception:
                u["period_start_balance"] = START_BALANCE

        # ensure fields exist
        u.setdefault("trades", 0)
        u.setdefault("wins", 0)
        u.setdefault("period_start_balance", START_BALANCE)
        u.setdefault("balance", START_BALANCE)

        u["last_update"] = _now_iso()
        _write_db(db)

        metrics = compute_user_metrics(u)
    return {
        "status": "ok",
        "user": {
            "user_id": user_id,
            "nickname": u.get("nickname", ""),
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"],
            "wins": metrics["wins"],
            "period_start_balance": metrics["period_start_balance"],
            "last_update": u.get("last_update")
        }
    }

@app.post("/api/user/{user_id}/trade")
def record_trade(user_id: str, tr: TradeRecord):
    """
    Record a trade outcome (win/lose). Optionally adjust balance by `amount`.
    - result: "win" or "lose"
    - amount: optional numeric; added to balance on win, subtracted on lose
    """
    with _lock:
        db = _read_db()
        users = db.setdefault("users", {})
        u = users.setdefault(user_id, {
            "nickname": "",
            "balance": START_BALANCE,
            "last_update": None,
            "trades": 0,
            "wins": 0,
            "period_start_balance": START_BALANCE
        })

        # normalize fields
        u.setdefault("trades", 0)
        u.setdefault("wins", 0)
        u.setdefault("period_start_balance", START_BALANCE)
        u.setdefault("balance", START_BALANCE)

        # apply trade
        res = tr.result.lower()
        if res not in ("win", "lose"):
            raise HTTPException(status_code=400, detail='result must be "win" or "lose"')

        u["trades"] = int(u.get("trades", 0)) + 1
        if res == "win":
            u["wins"] = int(u.get("wins", 0)) + 1

        # adjust balance when amount provided
        if tr.amount is not None:
            try:
                amt = float(tr.amount)
            except Exception:
                amt = 0.0
            if res == "win":
                u["balance"] = round(float(u.get("balance", START_BALANCE)) + amt, 2)
            else:
                u["balance"] = round(float(u.get("balance", START_BALANCE)) - amt, 2)

        u["last_update"] = _now_iso()
        _write_db(db)

        metrics = compute_user_metrics(u)

    return {
        "status": "ok",
        "user": {
            "user_id": user_id,
            "nickname": u.get("nickname", ""),
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"],
            "wins": metrics["wins"],
            "period_start_balance": metrics["period_start_balance"],
            "last_update": u.get("last_update")
        }
    }

@app.post("/api/close_month")
def post_close_month():
    """Manual trigger to close previous month and store podium snapshot."""
    with _lock:
        db = _read_db()
        prev_month = _prev_month_key()
        if prev_month in db.get("monthly_winners", {}):
            return {"status": "already_closed", "month": prev_month}
        podium = compute_podium_snapshot(db.get("users", {}), top_n=3)
        db.setdefault("monthly_winners", {})[prev_month] = {"podium": podium, "closed_at": _now_iso()}
        db["last_month_closed"] = _get_month_key()  # mark we've processed this month
        _write_db(db)
    return {"status": "closed", "month": prev_month, "podium": podium}

@app.get("/api/winners/{month}")
def get_winners(month: str):
    """Get winners for a specific month (YYYY-MM)."""
    with _lock:
        db = _read_db()
    winners = db.get("monthly_winners", {}).get(month)
    if not winners:
        raise HTTPException(status_code=404, detail="No winners for that month")
    return {"month": month, "winners": winners}

@app.get("/api/winners")
def get_latest_winners():
    """Return latest stored winners or empty list."""
    with _lock:
        db = _read_db()
    mw = db.get("monthly_winners", {})
    if not mw:
        return {"latest": None, "monthly_winners": {}}
    last_month = sorted(mw.keys())[-1]
    return {"latest": last_month, "winners": mw[last_month], "monthly_winners": mw}
