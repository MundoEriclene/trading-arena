import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any, List

# raiz do projeto (…/trading-arena)
APP_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = APP_ROOT / "backend" / "db" / "game.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                code TEXT PRIMARY KEY,
                nick TEXT NOT NULL,
                cash REAL NOT NULL,
                pos REAL NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                ts INTEGER NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                qty REAL NOT NULL,
                price REAL NOT NULL,
                notional REAL NOT NULL,
                fee REAL NOT NULL,
                cash_after REAL NOT NULL,
                pos_after REAL NOT NULL,
                FOREIGN KEY(code) REFERENCES players(code)
            );

            CREATE TABLE IF NOT EXISTS candles (
                ts INTEGER PRIMARY KEY,             -- início do candle (unix seconds)
                open REAL NOT NULL,
                high REAL NOT NULL,
                low  REAL NOT NULL,
                close REAL NOT NULL
            );

            -- estado do mercado (para reinício limpo)
            CREATE TABLE IF NOT EXISTS market_state (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL
            );
            """
        )


def get_state(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT v FROM market_state WHERE k = ?", (key,)).fetchone()
        return str(row["v"]) if row else None


def set_state(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO market_state(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )


def upsert_player(code: str, nick: str, initial_cash: float, now: int) -> None:
    """
    Se não existir, cria com cash=initial_cash e pos=0.
    Se existir, apenas atualiza nick e updated_at (NÃO reseta saldo).
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO players(code, nick, cash, pos, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
              nick=excluded.nick,
              updated_at=excluded.updated_at
            """,
            (code, nick, float(initial_cash), 0.0, int(now), int(now)),
        )


def get_player(code: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT code, nick, cash, pos, created_at, updated_at FROM players WHERE code = ?",
            (code,),
        ).fetchone()
        return dict(row) if row else None


def update_player_wallet(code: str, cash: float, pos: float, now: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE players SET cash=?, pos=?, updated_at=? WHERE code=?",
            (float(cash), float(pos), int(now), code),
        )


def insert_trade(
    code: str,
    ts: int,
    side: str,
    qty: float,
    price: float,
    notional: float,
    fee: float,
    cash_after: float,
    pos_after: float,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO trades(code, ts, side, qty, price, notional, fee, cash_after, pos_after)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                code,
                int(ts),
                side,
                float(qty),
                float(price),
                float(notional),
                float(fee),
                float(cash_after),
                float(pos_after),
            ),
        )


def list_recent_trades(code: str, limit: int = 20) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, ts, side, qty, price, notional, fee, cash_after, pos_after
            FROM trades
            WHERE code = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (code, int(limit)),
        ).fetchall()
        data = [dict(r) for r in rows]
        data.reverse()
        return data


def upsert_candle(ts: int, o: float, h: float, l: float, c: float) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO candles(ts, open, high, low, close)
            VALUES(?,?,?,?,?)
            ON CONFLICT(ts) DO UPDATE SET
              open=excluded.open,
              high=excluded.high,
              low=excluded.low,
              close=excluded.close
            """,
            (int(ts), float(o), float(h), float(l), float(c)),
        )


def get_last_candle() -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT ts, open, high, low, close FROM candles ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_candles_since(ts_from: int, limit: int = 600) -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close
            FROM candles
            WHERE ts >= ?
            ORDER BY ts ASC
            LIMIT ?
            """,
            (int(ts_from), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


