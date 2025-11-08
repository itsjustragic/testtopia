# main.py
"""
FastAPI leaderboard backend for Kenzies Fridge.

Usage:
    pip install fastapi uvicorn
    python -m uvicorn main:app --reload
"""
import os
import json
import threading
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import logging

logger = logging.getLogger(__name__)

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
            "users": {},
            "monthly_winners": {},
            "last_month_closed": None,
            "recent_trades": []
        }
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    with open(DB_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            # Corrupt file fallback
            return {
                "users": {},
                "monthly_winners": {},
                "last_month_closed": None,
                "recent_trades": []
            }

def _write_db(data: Dict[str, Any]):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        os.replace(tmp, DB_PATH)
    except Exception:
        # fallback
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
    try:
        balance = float(user_record.get("balance", START_BALANCE))
    except Exception:
        balance = START_BALANCE
    try:
        start = float(user_record.get("period_start_balance", START_BALANCE))
    except Exception:
        start = START_BALANCE

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
    trades: Optional[int] = None
    wins: Optional[int] = None
    period_start_balance: Optional[float] = None

class TradeRecord(BaseModel):
    result: str = Field(..., description='Either "win" or "lose"')
    amount: Optional[float] = None
    nickname: Optional[str] = None  # optional nickname to update on trade

app = FastAPI(title="Kenzies Fridge Leaderboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=FileResponse)
def root_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return JSONResponse({"message": "Kenzies Fridge API - static index not found"}, status_code=200)

@app.on_event("startup")
def startup_info():
    index_path = os.path.join(STATIC_DIR, "index.html")
    logger.info(f"Starting Kenzies Fridge API")
    logger.info(f"STATIC_DIR = {os.path.abspath(STATIC_DIR)}")
    logger.info(f"index.html exists: {os.path.exists(index_path)} -> {index_path}")
    try:
        files = os.listdir(STATIC_DIR)
        logger.info(f"static/*: {files[:30]}")
    except Exception as e:
        logger.info(f"Could not list static dir: {e}")

@app.get("/_debug/static-files")
def debug_static_files():
    index_path = os.path.join(STATIC_DIR, "index.html")
    exists = os.path.exists(index_path)
    try:
        listing = sorted(os.listdir(STATIC_DIR))
    except Exception as e:
        listing = f"error: {e}"
    return {
        "static_dir": os.path.abspath(STATIC_DIR),
        "index_exists": exists,
        "index_path": index_path,
        "listing_sample": listing[:200] if isinstance(listing, list) else listing,
    }

# ----------------------------
# Utilities: canonicalize & validate user ids
# ----------------------------
_USERID_RE = re.compile(r"^[a-z0-9\-_]{3,64}$")

def normalize_user_id(uid: str) -> str:
    if uid is None:
        return ""
    return str(uid).strip().lower()

def validate_user_id(uid: str) -> bool:
    if not uid:
        return False
    return bool(_USERID_RE.match(uid))

# ----------------------------
# API endpoints
# ----------------------------
@app.get("/api/leaderboard")
def get_leaderboard(limit: int = 100):
    with _lock:
        db = _read_db()
    users = db.get("users", {})
    arr = []
    for uid, u in users.items():
        metrics = compute_user_metrics(u)
        arr.append({
            "user_id": uid,
            "nickname": u.get("nickname", "") or "",
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"]
        })
    arr.sort(key=lambda x: x["balance"], reverse=True)
    return {"leaderboard": arr[:max(0, min(limit, 1000))], "timestamp": _now_iso()}

@app.get("/api/user/{user_id}")
def get_user(user_id: str):
    uid = normalize_user_id(user_id)
    if not validate_user_id(uid):
        raise HTTPException(status_code=400, detail="invalid user_id format")
    with _lock:
        db = _read_db()
    user = db.get("users", {}).get(uid)
    if user:
        metrics = compute_user_metrics(user)
        return {
            "user_id": uid,
            "nickname": user.get("nickname", "") or "",
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"],
            "wins": metrics["wins"],
            "period_start_balance": metrics["period_start_balance"],
            "last_update": user.get("last_update")
        }
    else:
        return {
            "user_id": uid,
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
def update_user(user_id: str, upd: UserUpdate):
    uid = normalize_user_id(user_id)
    if not validate_user_id(uid):
        raise HTTPException(status_code=400, detail="invalid user_id format")

    created = False
    changed_nick = None
    with _lock:
        db = _read_db()
        users = db.setdefault("users", {})

        # detect existence with canonical uid to avoid duplicates
        if uid not in users:
            # create once (canonicalized) - this avoids duplicate records for same id with different casing/whitespace
            users[uid] = {
                "nickname": "",
                "balance": START_BALANCE,
                "last_update": None,
                "trades": 0,
                "wins": 0,
                "period_start_balance": START_BALANCE
            }
            created = True

        u = users[uid]

        if upd.nickname is not None:
            n = (upd.nickname or "").strip()[:40]
            if n != u.get("nickname", ""):
                changed_nick = n
                u["nickname"] = n

        if upd.balance is not None:
            try:
                u["balance"] = round(float(upd.balance), 2)
            except Exception:
                u["balance"] = START_BALANCE

        if upd.trades is not None:
            try:
                u["trades"] = int(upd.trades)
            except Exception:
                u["trades"] = int(u.get("trades", 0) or 0)

        if upd.wins is not None:
            try:
                u["wins"] = int(upd.wins)
            except Exception:
                u["wins"] = int(u.get("wins", 0) or 0)

        if upd.period_start_balance is not None:
            try:
                u["period_start_balance"] = round(float(upd.period_start_balance), 2)
            except Exception:
                u["period_start_balance"] = START_BALANCE

        u["last_update"] = _now_iso()

        if changed_nick:
            recent = db.setdefault("recent_trades", [])
            for ent in recent:
                try:
                    if ent.get("user_id") == uid:
                        ent["nickname"] = changed_nick
                except Exception:
                    pass

        _write_db(db)
        metrics = compute_user_metrics(u)

    resp = {
        "status": "created" if created else "ok",
        "user": {
            "user_id": uid,
            "nickname": u.get("nickname", "") or "",
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"],
            "wins": metrics["wins"],
            "period_start_balance": metrics["period_start_balance"],
            "last_update": u.get("last_update")
        }
    }
    if changed_nick:
        resp["message"] = f"nickname set to {changed_nick}"
    return resp

@app.post("/api/user/{user_id}/trade")
def record_trade(user_id: str, tr: TradeRecord):
    uid = normalize_user_id(user_id)
    if not validate_user_id(uid):
        raise HTTPException(status_code=400, detail="invalid user_id format")

    changed_nick = None
    created = False
    with _lock:
        db = _read_db()
        users = db.setdefault("users", {})

        # ensure canonical user exists exactly once
        if uid not in users:
            users[uid] = {
                "nickname": "",
                "balance": START_BALANCE,
                "last_update": None,
                "trades": 0,
                "wins": 0,
                "period_start_balance": START_BALANCE
            }
            created = True

        u = users[uid]

        if tr.nickname is not None:
            n = (tr.nickname or "").strip()[:40]
            if n != u.get("nickname", ""):
                changed_nick = n
                u["nickname"] = n
                recent = db.setdefault("recent_trades", [])
                for ent in recent:
                    try:
                        if ent.get("user_id") == uid:
                            ent["nickname"] = changed_nick
                    except Exception:
                        pass

        u.setdefault("trades", 0)
        u.setdefault("wins", 0)
        u.setdefault("period_start_balance", START_BALANCE)
        u.setdefault("balance", START_BALANCE)

        res = tr.result.lower()
        if res not in ("win", "lose"):
            raise HTTPException(status_code=400, detail='result must be "win" or "lose"')

        u["trades"] = int(u.get("trades", 0)) + 1
        if res == "win":
            u["wins"] = int(u.get("wins", 0)) + 1

        amt = 0.0
        if tr.amount is not None:
            try:
                amt = float(tr.amount)
            except Exception:
                amt = 0.0

        if tr.amount is not None:
            if res == "win":
                u["balance"] = round(float(u.get("balance", START_BALANCE)) + amt, 2)
            else:
                u["balance"] = round(float(u.get("balance", START_BALANCE)) - amt, 2)

        u["last_update"] = _now_iso()

        trade_entry = {
            "ts": u["last_update"],
            "user_id": uid,
            "nickname": u.get("nickname", "") or "",
            "result": res,
            "amount": round(amt, 2)
        }
        recent = db.setdefault("recent_trades", [])
        recent.insert(0, trade_entry)
        MAX_RECENT_TRADES = 500
        if len(recent) > MAX_RECENT_TRADES:
            del recent[MAX_RECENT_TRADES:]

        _write_db(db)

        metrics = compute_user_metrics(u)

    resp = {
        "status": "created" if created else "ok",
        "user": {
            "user_id": uid,
            "nickname": u.get("nickname", "") or "",
            "balance": metrics["balance"],
            "performance": metrics["performance"],
            "win_rate": metrics["win_rate"],
            "trades_this_period": metrics["trades_this_period"],
            "wins": metrics["wins"],
            "period_start_balance": metrics["period_start_balance"],
            "last_update": u.get("last_update")
        }
    }
    if changed_nick:
        resp["message"] = f"nickname set to {changed_nick}"
    return resp

@app.get("/api/live-wins")
def get_live_wins(limit: int = 100, minutes: Optional[int] = None, nickname: Optional[str] = None):
    if limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be > 0")
    limit = min(limit, 500)

    with _lock:
        db = _read_db()
        recent = list(db.get("recent_trades", []))

    cutoff = None
    if minutes is not None:
        try:
            minutes = int(minutes)
            cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid minutes parameter")

    def parse_ts(s):
        try:
            if s.endswith("Z"):
                s2 = s[:-1]
            else:
                s2 = s
            return datetime.fromisoformat(s2)
        except Exception:
            return None

    filtered = []
    lower_filter = nickname.lower().strip() if nickname else None

    for entry in recent:
        if cutoff:
            ts = parse_ts(entry.get("ts", ""))
            if not ts or ts < cutoff:
                continue
        if lower_filter:
            if (entry.get("nickname") or "").strip().lower() != lower_filter:
                continue
        filtered.append(entry)
        if len(filtered) >= limit:
            break

    summary = {}
    for e in filtered:
        nick = (e.get("nickname") or "")[:40]
        key = nick if nick.strip() else 'Anon'
        s = summary.setdefault(key, {"net": 0.0, "wins": 0, "losses": 0, "trades": 0})
        amt = float(e.get("amount", 0.0) or 0.0)
        if e.get("result") == "win":
            s["net"] = round(s["net"] + amt, 2)
            s["wins"] += 1
        else:
            s["net"] = round(s["net"] - amt, 2)
            s["losses"] += 1
        s["trades"] += 1

    return {
        "recent_trades": filtered,
        "summary": summary,
        "timestamp": _now_iso()
    }

@app.post("/api/close_month")
def post_close_month():
    with _lock:
        db = _read_db()
        prev_month = _prev_month_key()
        if prev_month in db.get("monthly_winners", {}):
            return {"status": "already_closed", "month": prev_month}
        podium = compute_podium_snapshot(db.get("users", {}), top_n=3)
        db.setdefault("monthly_winners", {})[prev_month] = {"podium": podium, "closed_at": _now_iso()}
        db["last_month_closed"] = _get_month_key()
        _write_db(db)
    return {"status": "closed", "month": prev_month, "podium": podium}

@app.get("/api/winners/{month}")
def get_winners(month: str):
    with _lock:
        db = _read_db()
    winners = db.get("monthly_winners", {}).get(month)
    if not winners:
        raise HTTPException(status_code=404, detail="No winners for that month")
    return {"month": month, "winners": winners}

@app.get("/api/winners")
def get_latest_winners():
    with _lock:
        db = _read_db()
    mw = db.get("monthly_winners", {})
    if not mw:
        return {"latest": None, "monthly_winners": {}}
    last_month = sorted(mw.keys())[-1]
    return {"latest": last_month, "winners": mw[last_month], "monthly_winners": mw}
