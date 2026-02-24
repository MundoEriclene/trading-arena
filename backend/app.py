import time
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Tuple, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend import db
from backend.market import engine


APP_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = APP_ROOT / "frontend"
DB_FILE = APP_ROOT / "backend" / "db" / "game.db"

INITIAL_CASH = 10_000.0

# GitHub Pages (prod) + localhost (dev)
ALLOWED_ORIGINS = [
    "https://mundoericlene.github.io",
    "https://mundoericlene.github.io/trading-arena",
    "http://localhost",
    "http://localhost:3000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
]

# Cache simples para stats por jogador (evita recalcular toda hora lendo todos os trades)
# Estrutura: code -> (last_trade_id, avg, realized, pos_at_calc)
_STATS_CACHE: Dict[str, Tuple[int, float, float, float]] = {}
_STATS_CACHE_TTL_SEC = 2.0
_STATS_CACHE_TS: Dict[str, float] = {}


# ---------- SQLite helpers ----------
def _conn() -> sqlite3.Connection:
    # timeout + check_same_thread=False ajudam em concorrência (muitos requests)
    c = sqlite3.connect(DB_FILE, timeout=30.0, check_same_thread=False)
    c.row_factory = sqlite3.Row

    # PRAGMAs para reduzir travamentos e melhorar leitura concorrente
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("PRAGMA temp_store=MEMORY;")
        c.execute("PRAGMA busy_timeout=5000;")  # 5s
    except Exception:
        # se falhar por algum motivo, não derruba API
        pass

    return c


def _list_players(limit: int = 200) -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT code, nick, cash, pos, created_at, updated_at
            FROM players
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _last_candles_raw(limit_rows: int = 5000) -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close
            FROM candles
            ORDER BY ts DESC
            LIMIT ?
            """,
            (limit_rows,),
        ).fetchall()
        data = [dict(r) for r in rows]
        data.reverse()
        return data


def _list_trades_recent(code: str, limit: int = 50) -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, ts, side, qty, price, notional, fee, cash_after, pos_after
            FROM trades
            WHERE code = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (code, limit),
        ).fetchall()
        data = [dict(r) for r in rows]
        data.reverse()
        return data


def _list_trades_asc(code: str) -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, ts, side, qty, price, notional, fee
            FROM trades
            WHERE code = ?
            ORDER BY id ASC
            """,
            (code,),
        ).fetchall()
        return [dict(r) for r in rows]


def _last_trade_id(code: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS last_id FROM trades WHERE code = ?",
            (code,),
        ).fetchone()
        return int(row["last_id"] if row else 0)


def _compute_stats_from_trades(code: str) -> Tuple[float, float, float]:
    """
    LONG + SHORT:
      pos > 0  => LONG
      pos < 0  => SHORT
    avg_price = preço médio da posição atual (sempre >= 0)
    realized_pnl acumula ao reduzir/fechar posição
    """
    # Cache por code (para leaderboard e /me não recalcularem toda hora)
    now = time.time()
    ttl_ok = (now - _STATS_CACHE_TS.get(code, 0.0)) <= _STATS_CACHE_TTL_SEC
    last_id = _last_trade_id(code)

    cached = _STATS_CACHE.get(code)
    if ttl_ok and cached and cached[0] == last_id:
        _, avg, realized, pos = cached
        return float(avg), float(realized), float(pos)

    trades = _list_trades_asc(code)

    pos = 0.0
    avg = 0.0
    realized = 0.0

    for t in trades:
        side = str(t["side"]).upper()
        qty = float(t["qty"])
        price = float(t["price"])
        fee = float(t.get("fee") or 0.0)

        if side == "BUY":
            if pos >= 0:
                new_pos = pos + qty
                avg = (pos * avg + qty * price) / new_pos if new_pos != 0 else 0.0
                pos = new_pos
            else:
                cover = min(qty, abs(pos))
                realized += (avg - price) * cover
                pos += cover  # pos é negativo
                leftover = qty - cover
                if abs(pos) < 1e-12:
                    pos = 0.0
                    avg = 0.0
                if leftover > 0:
                    pos = leftover
                    avg = price

            realized -= fee

        elif side == "SELL":
            if pos <= 0:
                new_pos = pos - qty
                abs_old = abs(pos)
                abs_new = abs(new_pos)
                avg = (abs_old * avg + qty * price) / abs_new if abs_new != 0 else 0.0
                pos = new_pos
            else:
                close = min(qty, pos)
                realized += (price - avg) * close
                pos -= close
                leftover = qty - close
                if abs(pos) < 1e-12:
                    pos = 0.0
                    avg = 0.0
                if leftover > 0:
                    pos = -leftover
                    avg = price

            realized -= fee

    _STATS_CACHE[code] = (last_id, float(avg), float(realized), float(pos))
    _STATS_CACHE_TS[code] = now
    return float(avg), float(realized), float(pos)


def _aggregate_candles(raw: List[Dict[str, Any]], tf_seconds: int) -> List[Dict[str, Any]]:
    if not raw:
        return []

    tf = max(1, int(tf_seconds))
    out: List[Dict[str, Any]] = []

    cur_bucket: Optional[int] = None
    o = h = l = c = None  # type: ignore

    for r in raw:
        ts = int(r["ts"])
        bucket = (ts // tf) * tf
        ro = float(r["open"])
        rh = float(r["high"])
        rl = float(r["low"])
        rc = float(r["close"])

        if cur_bucket is None:
            cur_bucket = bucket
            o, h, l, c = ro, rh, rl, rc
            continue

        if bucket != cur_bucket:
            out.append({"time": int(cur_bucket), "open": o, "high": h, "low": l, "close": c})
            cur_bucket = bucket
            o, h, l, c = ro, rh, rl, rc
        else:
            h = max(h, rh)
            l = min(l, rl)
            c = rc

    if cur_bucket is not None:
        out.append({"time": int(cur_bucket), "open": o, "high": h, "low": l, "close": c})

    return out


# ---------- API Models ----------
class JoinReq(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)
    nick: str = Field(..., min_length=1, max_length=32)


class TradeReq(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)
    side: Literal["BUY", "SELL"]
    usd: float = Field(..., gt=0)


# ---------- FastAPI ----------
app = FastAPI(title="Trading Arena - AMM (Seed + Short)")

# CORS obrigatório para GitHub Pages -> Tunnel (browser)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # sem cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=600,
)

# Static serve só para DEV local (não atrapalha prod)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.middleware("http")
async def add_basic_headers(request: Request, call_next):
    resp = await call_next(request)
    # Segurança + evitar caching indevido de state/live
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.on_event("startup")
def on_startup():
    db.init_db()
    engine.start()


# =========================================================
# HEALTH
# =========================================================
@app.get("/api/health")
def health():
    # útil para testar o tunnel e uptime
    return {"ok": True, "ts": int(time.time())}


@app.get("/")
def home():
    # útil só quando rodar tudo local (não é usado no GitHub Pages)
    if (FRONTEND_DIR / "index.html").exists():
        return FileResponse(str(FRONTEND_DIR / "index.html"))
    return {"ok": True, "hint": "Frontend está no GitHub Pages. Use /api/* para dados."}


@app.get("/favicon.ico")
def favicon():
    return JSONResponse({}, status_code=204)


# =========================================================
# GAME CONTROL
# =========================================================
@app.post("/api/start")
def start_game():
    return JSONResponse(engine.start_game())


@app.get("/api/state")
def state():
    return JSONResponse(engine.snapshot())


# =========================================================
# PLAYER
# =========================================================
@app.post("/api/join")
def join(payload: JoinReq):
    now = int(time.time())
    code = payload.code.strip()
    nick = payload.nick.strip()

    if " " in code:
        raise HTTPException(status_code=400, detail="Código não pode ter espaços.")
    if not nick:
        raise HTTPException(status_code=400, detail="Nick inválido.")

    db.upsert_player(code=code, nick=nick, initial_cash=INITIAL_CASH, now=now)
    return {"ok": True, "code": code, "nick": nick, "initial_cash": INITIAL_CASH}


@app.get("/api/me")
def me(code: str):
    p = db.get_player(code)
    if not p:
        raise HTTPException(status_code=404, detail="Jogador não encontrado. Faça join.")

    price = float(engine.current_price())
    cash = float(p["cash"])
    pos = float(p["pos"])
    equity = cash + pos * price

    avg_price, pnl_realized, pos_calc = _compute_stats_from_trades(code)

    # unrealized (long ou short)
    if pos > 0 and avg_price > 0:
        pnl_unrealized = (price - avg_price) * pos
    elif pos < 0 and avg_price > 0:
        pnl_unrealized = (avg_price - price) * abs(pos)
    else:
        pnl_unrealized = 0.0

    pnl_total = pnl_realized + pnl_unrealized

    return JSONResponse(
        {
            "ok": True,
            "code": p["code"],
            "nick": p["nick"],
            "cash": cash,
            "pos": pos,
            "price": price,
            "equity": equity,
            "avg_price": avg_price,
            "pnl_realized": pnl_realized,
            "pnl_unrealized": pnl_unrealized,
            "pnl_total": pnl_total,
            "pos_calc": pos_calc,  # debug opcional (pode usar no front se quiser)
        }
    )


# =========================================================
# MARKET (AMM)
# =========================================================
@app.post("/api/trade")
def trade(payload: TradeReq):
    code = payload.code.strip()
    side = payload.side.upper()
    usd = float(payload.usd)

    p = db.get_player(code)
    if not p:
        raise HTTPException(status_code=404, detail="Jogador não encontrado. Faça join.")

    if side == "BUY":
        res = engine.market_buy(code, usd)
    elif side == "SELL":
        # SELL em USD => converte para qty RICH pela price atual e vende (pode abrir short)
        price_now = float(engine.current_price())
        rich_qty = usd / max(0.0001, price_now)
        res = engine.market_sell(code, rich_qty)
    else:
        raise HTTPException(status_code=400, detail="Side inválido.")

    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Trade recusado"))

    # invalida cache do jogador (garante stats atualizados logo após trade)
    _STATS_CACHE_TS[code] = 0.0

    # devolve "me" atualizado para o frontend (PnL + equity)
    p2 = db.get_player(code)
    price = float(engine.current_price())
    cash = float(p2["cash"])
    pos = float(p2["pos"])
    equity = cash + pos * price

    avg_price, pnl_realized, _ = _compute_stats_from_trades(code)
    if pos > 0 and avg_price > 0:
        pnl_unrealized = (price - avg_price) * pos
    elif pos < 0 and avg_price > 0:
        pnl_unrealized = (avg_price - price) * abs(pos)
    else:
        pnl_unrealized = 0.0
    pnl_total = pnl_realized + pnl_unrealized

    res["me"] = {
        "cash": cash,
        "pos": pos,
        "price": price,
        "equity": equity,
        "avg_price": avg_price,
        "pnl_realized": pnl_realized,
        "pnl_unrealized": pnl_unrealized,
        "pnl_total": pnl_total,
    }

    return JSONResponse(res)


# =========================================================
# TRADES / HISTORY
# =========================================================
@app.get("/api/trades")
def trades(code: str, limit: int = 50):
    p = db.get_player(code)
    if not p:
        raise HTTPException(status_code=404, detail="Jogador não encontrado.")
    limit = max(1, min(int(limit), 200))
    return JSONResponse(_list_trades_recent(code, limit=limit))


# =========================================================
# CANDLES (5m default) + LIVE 1s agregado
# =========================================================
@app.get("/api/candles")
def candles(limit: int = 200, tf: int = 300):
    limit = max(10, min(int(limit), 2000))
    tf = max(1, min(int(tf), 3600 * 24))

    # evitar explodir DB: limita o quanto pode buscar
    # regra: no máximo 60k linhas (já tinha), mas garantimos mínimo coerente
    need_rows = int(limit * tf)
    need_rows = max(500, min(need_rows, 60000))

    raw = _last_candles_raw(limit_rows=need_rows)

    # injeta candle live do engine (1s) para o ultimo bucket ficar vivo
    snap = engine.snapshot()
    live = snap.get("candle") or {}
    if live and "ts" in live:
        live_row = {
            "ts": int(live["ts"]),
            "open": float(live["open"]),
            "high": float(live["high"]),
            "low": float(live["low"]),
            "close": float(live["close"]),
        }
        if not raw or int(raw[-1]["ts"]) != live_row["ts"]:
            raw.append(live_row)
        else:
            raw[-1] = live_row

    agg = _aggregate_candles(raw, tf_seconds=tf)
    if len(agg) > limit:
        agg = agg[-limit:]

    return JSONResponse(agg)


# =========================================================
# LEADERBOARD (com PnL)
# =========================================================
@app.get("/api/leaderboard")
def leaderboard(limit: int = 50):
    limit = max(1, min(int(limit), 500))
    price = float(engine.current_price())
    players = _list_players(limit=limit)

    rows = []
    for p in players:
        cash = float(p["cash"])
        pos = float(p["pos"])
        equity = cash + pos * price

        avg_price, pnl_realized, _ = _compute_stats_from_trades(p["code"])
        if pos > 0 and avg_price > 0:
            pnl_unrealized = (price - avg_price) * pos
        elif pos < 0 and avg_price > 0:
            pnl_unrealized = (avg_price - price) * abs(pos)
        else:
            pnl_unrealized = 0.0
        pnl_total = pnl_realized + pnl_unrealized

        rows.append(
            {
                "nick": p["nick"],
                "equity": float(equity),
                "pnl": float(pnl_total),
                "pos": float(pos),
                "cash": float(cash),
            }
        )

    rows.sort(key=lambda x: x["equity"], reverse=True)
    return JSONResponse(rows)