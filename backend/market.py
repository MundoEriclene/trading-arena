import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

from backend import db


@dataclass
class MarketConfig:
    # Candle para UI em tempo real (1s no DB; o frontend agrega em 5m)
    candle_seconds: int = 1
    tick_seconds: float = 1.0

    # Preço inicial do ativo RICH/USD ao iniciar o jogo
    start_price: float = 100.0

    # Liquidez inicial do pool (define "profundidade" / slippage)
    initial_usd_liquidity: float = 200_000.0

    # Seed apenas visual (histórico)
    seed_enabled: bool = True

    # Queremos 1 semana de histórico "visível"
    seed_seconds: int = 7 * 24 * 60 * 60  # 7 dias

    # IMPORTANTÍSSIMO: seed mais leve (1 minuto) para não explodir DB
    # (live continua em 1s via candle_seconds)
    seed_candle_seconds: int = 60  # 1 minuto

    # Volatilidade visual do seed (por candle de seed, ou seja, por 1 minuto)
    seed_step_pct: float = 0.0007  # 0,07% por minuto (ajuste fino)

    # Fees (0 para ficar "sanguinário" e simples)
    fee_rate: float = 0.0

    # Margem / risco (para SHORT)
    # equity = cash + pos * price
    # Regra:
    #   - equity_after tem de ser >= min_equity
    #   - abs(pos_after)*price <= equity_after * leverage_max
    min_equity: float = 0.0
    leverage_max: float = 3.0

    # Stop-out (opcional)
    stopout_equity: float = 0.0


class MarketEngine:
    """
    Motor do mercado usando AMM (x*y=k) com LONG + SHORT.

    Convenção:
      - pos > 0  => LONG (tem RICH)
      - pos = 0  => FLAT
      - pos < 0  => SHORT (vendeu RICH "emprestado")

    Importante:
      - Depois do start_game(), preço só muda por trades.
      - Seed é só visual: não cria trades nem mexe no pool.
      - Seed de 1 semana é gerado em 1 MINUTO para performance.
    """

    STATE_PRICE = "price"
    STATE_CANDLE_TS = "candle_ts"
    STATE_POOL_X = "pool_x"
    STATE_POOL_Y = "pool_y"
    STATE_POOL_K = "pool_k"
    STATE_STARTED = "started"

    # versão do seed (para não “prender” no seed antigo)
    STATE_SEEDED_TAG = "seeded_tag"

    def __init__(self, cfg: MarketConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # AMM pool
        self.pool_x: float = 0.0  # RICH reserve
        self.pool_y: float = 0.0  # USD reserve
        self.pool_k: float = 0.0

        # price
        self.price: float = float(cfg.start_price)

        # candle 1s (OHLC) para lightweight-charts
        self.candle_ts: int = 0
        self.candle_o: float = self.price
        self.candle_h: float = self.price
        self.candle_l: float = self.price
        self.candle_c: float = self.price

        # started gate
        self.started: bool = False

    # ---------- DB helpers ----------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(
            str(db.DB_PATH),
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        c.row_factory = sqlite3.Row
        return c

    def _get_state_float(self, key: str) -> Optional[float]:
        v = db.get_state(key)
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    def _set_pool_state(self) -> None:
        db.set_state(self.STATE_POOL_X, str(self.pool_x))
        db.set_state(self.STATE_POOL_Y, str(self.pool_y))
        db.set_state(self.STATE_POOL_K, str(self.pool_k))
        db.set_state(self.STATE_PRICE, str(self.price))
        db.set_state(self.STATE_CANDLE_TS, str(self.candle_ts))
        db.set_state(self.STATE_STARTED, "1" if self.started else "0")

    @staticmethod
    def _equity(cash: float, pos: float, price: float) -> float:
        return float(cash + pos * price)

    def _margin_ok(self, cash_after: float, pos_after: float, price_after: float) -> bool:
        equity = self._equity(cash_after, pos_after, price_after)
        if equity < float(self.cfg.min_equity):
            return False

        lev = float(self.cfg.leverage_max)
        if lev <= 0:
            return False

        exposure = abs(pos_after) * price_after  # em USD
        max_exposure = equity * lev
        return exposure <= (max_exposure + 1e-9)

    # ---------- Seed (histórico visual) ----------
    def _seed_tag(self) -> str:
        # se mudar configs do seed, muda a tag automaticamente
        return f"v2|secs={int(self.cfg.seed_seconds)}|cs={int(self.cfg.seed_candle_seconds)}|step={float(self.cfg.seed_step_pct):.8f}|p0={float(self.cfg.start_price):.6f}"

    def _get_earliest_candle_ts(self) -> Optional[int]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT ts FROM candles ORDER BY ts ASC LIMIT 1").fetchone()
            return int(row["ts"]) if row else None
        finally:
            conn.close()

    def _get_latest_candle_ts(self) -> Optional[int]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT ts FROM candles ORDER BY ts DESC LIMIT 1").fetchone()
            return int(row["ts"]) if row else None
        finally:
            conn.close()

    def seed_history_if_needed(self) -> None:
        """
        Gera/estende um histórico visual (random-walk suave) para cobrir 1 semana,
        usando candles de seed em 1 minuto (leve).

        Regras do seed:
          - NÃO cria trades
          - NÃO altera pool (pool só inicializa no start)
          - NÃO destrói candles existentes: apenas estende para trás se faltar histórico.
        """
        if not self.cfg.seed_enabled:
            return

        now = int(time.time())
        target_start = now - int(self.cfg.seed_seconds)

        seed_cs = max(1, int(self.cfg.seed_candle_seconds))
        target_start = (target_start // seed_cs) * seed_cs

        # Se já temos histórico suficiente, não faz nada
        earliest = self._get_earliest_candle_ts()
        if earliest is not None and earliest <= target_start:
            # já cobre 1 semana (ou mais)
            db.set_state(self.STATE_SEEDED_TAG, self._seed_tag())
            return

        # Define o ponto final do seed (onde vamos parar)
        # - se já existe candle, paramos no earliest atual (para não colidir)
        # - se não existe nada, paramos em "now" alinhado
        end_ts = earliest if earliest is not None else ((now // seed_cs) * seed_cs)

        # Se for o primeiro seed, usa start_price como base.
        # Se já existirem candles, usamos o close do primeiro candle existente como âncora.
        last_close = float(self.cfg.start_price)
        if earliest is not None:
            # vamos “andar” até chegar no earliest; para isso precisamos de uma âncora
            # pegamos o open do earliest candle como referência (mais coerente)
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT open FROM candles WHERE ts = ? LIMIT 1", (earliest,)
                ).fetchone()
                if row:
                    last_close = float(row["open"])
            finally:
                conn.close()

        # Vamos gerar do target_start até end_ts (exclusivo), em seed_cs
        # Random-walk com leve mean-reversion para não “fugir” do range.
        # Observação: como estamos gerando para trás (antes do earliest), usamos uma
        # lógica de drift normal, mas apenas gravamos candles para trás; visualmente fica bom.
        for ts in range(target_start, int(end_ts), seed_cs):
            step = random.uniform(-1.0, 1.0) * float(self.cfg.seed_step_pct)

            # mean-reversion suave em torno do start_price
            mr = (float(self.cfg.start_price) - last_close) / float(self.cfg.start_price) * 0.015

            ret = step + mr
            close = max(0.0001, last_close * (1.0 + ret))

            o = last_close
            h = max(o, close)
            l = min(o, close)

            # grava candle (ts é a “abertura” do bucket do seed)
            db.upsert_candle(ts=int(ts), o=float(o), h=float(h), l=float(l), c=float(close))

            last_close = close

        db.set_state(self.STATE_SEEDED_TAG, self._seed_tag())

    # ---------- Lifecycle ----------
    def init_or_load(self) -> None:
        db.init_db()

        # 1) Seed visual (opcional) — agora 1 semana leve
        self.seed_history_if_needed()

        # 2) Carrega último candle para price inicial do UI
        last = db.get_last_candle()
        with self._lock:
            if last:
                self.price = float(last["close"])
            else:
                self.price = float(self.cfg.start_price)

            now = int(time.time())
            cs = max(1, int(self.cfg.candle_seconds))
            self.candle_ts = (now // cs) * cs
            self.candle_o = self.price
            self.candle_h = self.price
            self.candle_l = self.price
            self.candle_c = self.price

            st = db.get_state(self.STATE_STARTED)
            self.started = (st == "1")

            # Recarrega pool (para reboot)
            x = self._get_state_float(self.STATE_POOL_X)
            y = self._get_state_float(self.STATE_POOL_Y)
            k = self._get_state_float(self.STATE_POOL_K)
            if x and y and k and x > 0 and y > 0 and k > 0:
                self.pool_x = float(x)
                self.pool_y = float(y)
                self.pool_k = float(k)
                self.price = float(self.pool_y / self.pool_x)

            db.set_state(self.STATE_PRICE, str(self.price))
            db.set_state(self.STATE_CANDLE_TS, str(self.candle_ts))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def start_game(self) -> Dict[str, Any]:
        """
        Inicializa o AMM pool e ativa started=1.
        Depois disso, preço só muda por trades.
        """
        with self._lock:
            if self.started and self.pool_x > 0 and self.pool_y > 0:
                return self.snapshot()

            usd_liq = max(1000.0, float(self.cfg.initial_usd_liquidity))
            p0 = max(0.0001, float(self.price))
            x = usd_liq / p0
            y = usd_liq
            k = x * y

            self.pool_x = float(x)
            self.pool_y = float(y)
            self.pool_k = float(k)

            self.price = float(self.pool_y / self.pool_x)
            self.started = True

            now = int(time.time())
            cs = max(1, int(self.cfg.candle_seconds))
            self.candle_ts = (now // cs) * cs
            self.candle_o = self.price
            self.candle_h = self.price
            self.candle_l = self.price
            self.candle_c = self.price

            self._set_pool_state()

        return self.snapshot()

    # ---------- Public ----------
    def current_price(self) -> float:
        with self._lock:
            return float(self.price)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "started": self.started,
                "price": float(self.price),
                "pool": {
                    "x_rich": float(self.pool_x),
                    "y_usd": float(self.pool_y),
                    "k": float(self.pool_k),
                },
                "candle": {
                    "ts": int(self.candle_ts),
                    "open": float(self.candle_o),
                    "high": float(self.candle_h),
                    "low": float(self.candle_l),
                    "close": float(self.candle_c),
                },
            }

    # ---------- Core: Market Orders ----------
    def market_buy(self, code: str, usd_in: float) -> Dict[str, Any]:
        """
        BUY market:
          - Player paga usd_in
          - Recebe rich_out do pool
          - Pode também fechar SHORT (pos negativa) automaticamente.
        """
        code = str(code).strip()
        usd_in = float(usd_in)
        if not code:
            return {"ok": False, "error": "code inválido"}
        if usd_in <= 0:
            return {"ok": False, "error": "usd_in inválido"}

        now = int(time.time())
        conn = self._conn()
        try:
            with self._lock:
                if not self.started:
                    return {"ok": False, "error": "mercado não iniciado (start_game)"}
                if self.pool_x <= 0 or self.pool_y <= 0 or self.pool_k <= 0:
                    return {"ok": False, "error": "pool inválido"}

                row = conn.execute(
                    "SELECT cash, pos FROM players WHERE code = ?",
                    (code,),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": "player não existe"}

                cash = float(row["cash"])
                pos = float(row["pos"])

                if cash < usd_in:
                    return {"ok": False, "error": "saldo USD insuficiente"}

                fee = usd_in * float(self.cfg.fee_rate)
                usd_effective = usd_in - fee
                if usd_effective <= 0:
                    return {"ok": False, "error": "usd_in pequeno demais (fee)"}

                # AMM: Y' = Y + usd_effective ; X' = K / Y'
                y_new = self.pool_y + usd_effective
                x_new = self.pool_k / y_new
                rich_out = self.pool_x - x_new

                if rich_out <= 0 or x_new <= 0:
                    return {"ok": False, "error": "liquidez insuficiente"}

                # Atualiza pool / preço
                self.pool_y = float(y_new)
                self.pool_x = float(x_new)
                self.price = float(self.pool_y / self.pool_x)

                trade_price = float(usd_effective / rich_out)
                notional = float(usd_in)

                cash_after = cash - usd_in
                pos_after = pos + rich_out  # se pos era negativa (short), isto cobre

                # margem após
                if not self._margin_ok(cash_after, pos_after, self.price):
                    # reverte pool (porque estamos sob lock)
                    self.pool_y -= float(usd_effective)
                    self.pool_x += float(rich_out)
                    self.price = float(self.pool_y / self.pool_x)
                    return {"ok": False, "error": "margem insuficiente / alavancagem excedida"}

                conn.execute(
                    "UPDATE players SET cash = ?, pos = ?, updated_at = ? WHERE code = ?",
                    (cash_after, pos_after, now, code),
                )
                conn.execute(
                    """
                    INSERT INTO trades (code, ts, side, qty, price, notional, fee, cash_after, pos_after)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (code, now, "BUY", float(rich_out), float(trade_price), float(notional), float(fee), float(cash_after), float(pos_after)),
                )

                self._touch_candle(now, self.price)
                self._set_pool_state()

            conn.commit()

            return {
                "ok": True,
                "side": "BUY",
                "ts": now,
                "usd_in": float(usd_in),
                "fee": float(fee),
                "rich_out": float(rich_out),
                "avg_price": float(trade_price),
                "price_after": float(self.price),
                "cash_after": float(cash_after),
                "pos_after": float(pos_after),
            }
        finally:
            conn.close()

    def market_sell(self, code: str, rich_in: float) -> Dict[str, Any]:
        """
        SELL market (com SHORT):
          - Player vende rich_in (se tiver LONG) OU vende "emprestado" (abre short)
          - Recebe usd_out do pool
          - Preço cai conforme AMM
        """
        code = str(code).strip()
        rich_in = float(rich_in)
        if not code:
            return {"ok": False, "error": "code inválido"}
        if rich_in <= 0:
            return {"ok": False, "error": "rich_in inválido"}

        now = int(time.time())
        conn = self._conn()
        try:
            with self._lock:
                if not self.started:
                    return {"ok": False, "error": "mercado não iniciado (start_game)"}
                if self.pool_x <= 0 or self.pool_y <= 0 or self.pool_k <= 0:
                    return {"ok": False, "error": "pool inválido"}

                row = conn.execute(
                    "SELECT cash, pos FROM players WHERE code = ?",
                    (code,),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": "player não existe"}

                cash = float(row["cash"])
                pos = float(row["pos"])

                # AMM: X' = X + rich_in ; Y' = K / X'
                x_new = self.pool_x + rich_in
                y_new = self.pool_k / x_new
                usd_out_gross = self.pool_y - y_new

                if usd_out_gross <= 0 or y_new <= 0:
                    return {"ok": False, "error": "liquidez insuficiente"}

                fee = usd_out_gross * float(self.cfg.fee_rate)
                usd_out = usd_out_gross - fee
                if usd_out <= 0:
                    return {"ok": False, "error": "resultado pequeno demais (fee)"}

                # Atualiza pool / preço
                self.pool_x = float(x_new)
                self.pool_y = float(y_new)
                self.price = float(self.pool_y / self.pool_x)

                trade_price = float(usd_out / rich_in)
                notional = float(usd_out)

                # SHORT permitido
                cash_after = cash + usd_out
                pos_after = pos - rich_in  # pode ir negativo

                if not self._margin_ok(cash_after, pos_after, self.price):
                    # reverte pool
                    self.pool_x -= float(rich_in)
                    self.pool_y += float(usd_out_gross)
                    self.price = float(self.pool_y / self.pool_x)
                    return {"ok": False, "error": "margem insuficiente / alavancagem excedida"}

                conn.execute(
                    "UPDATE players SET cash = ?, pos = ?, updated_at = ? WHERE code = ?",
                    (cash_after, pos_after, now, code),
                )
                conn.execute(
                    """
                    INSERT INTO trades (code, ts, side, qty, price, notional, fee, cash_after, pos_after)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (code, now, "SELL", float(rich_in), float(trade_price), float(notional), float(fee), float(cash_after), float(pos_after)),
                )

                self._touch_candle(now, self.price)
                self._set_pool_state()

            conn.commit()

            return {
                "ok": True,
                "side": "SELL",
                "ts": now,
                "rich_in": float(rich_in),
                "fee": float(fee),
                "usd_out": float(usd_out),
                "avg_price": float(trade_price),
                "price_after": float(self.price),
                "cash_after": float(cash_after),
                "pos_after": float(pos_after),
            }
        finally:
            conn.close()

    # ---------- Candle handling ----------
    def _touch_candle(self, now_s: int, price: float) -> None:
        cs = max(1, int(self.cfg.candle_seconds))
        ts_bucket = (now_s // cs) * cs

        if ts_bucket != self.candle_ts:
            db.upsert_candle(
                ts=int(self.candle_ts),
                o=float(self.candle_o),
                h=float(self.candle_h),
                l=float(self.candle_l),
                c=float(self.candle_c),
            )
            self.candle_ts = int(ts_bucket)
            self.candle_o = float(price)
            self.candle_h = float(price)
            self.candle_l = float(price)
            self.candle_c = float(price)
        else:
            self.candle_c = float(price)
            if price > self.candle_h:
                self.candle_h = float(price)
            if price < self.candle_l:
                self.candle_l = float(price)

    # ---------- Loop (1s) ----------
    def _run_loop(self) -> None:
        self.init_or_load()

        next_tick = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now < next_tick:
                time.sleep(min(0.05, next_tick - now))
                continue

            self._tick()
            next_tick += float(self.cfg.tick_seconds)

    def _tick(self) -> None:
        """
        Tick a cada 1s:
          - NÃO mexe no preço.
          - Garante candle "flat" mesmo sem trades.
        """
        now_s = int(time.time())

        with self._lock:
            self._touch_candle(now_s, float(self.price))
            db.set_state(self.STATE_PRICE, str(self.price))
            db.set_state(self.STATE_CANDLE_TS, str(self.candle_ts))

        if float(self.cfg.stopout_equity) > 0:
            self._liquidate_if_needed()

    def _liquidate_if_needed(self) -> None:
        """
        Stop-out simplificado (opcional).
        """
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT code, cash, pos FROM players WHERE pos != 0"
            ).fetchall()
            if not rows:
                return

            now = int(time.time())
            with self._lock:
                mark = float(self.price)

            for r in rows:
                code = r["code"]
                cash = float(r["cash"])
                pos = float(r["pos"])
                equity = self._equity(cash, pos, mark)

                if equity <= float(self.cfg.stopout_equity):
                    side = "SELL" if pos > 0 else "BUY"
                    qty = abs(pos)
                    notional = qty * mark

                    cash_after = 0.0
                    pos_after = 0.0

                    conn.execute(
                        "UPDATE players SET cash = ?, pos = ?, updated_at = ? WHERE code = ?",
                        (cash_after, pos_after, now, code),
                    )
                    conn.execute(
                        """
                        INSERT INTO trades (code, ts, side, qty, price, notional, fee, cash_after, pos_after)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (code, now, side, float(qty), float(mark), float(notional), 0.0, float(cash_after), float(pos_after)),
                    )

            conn.commit()
        finally:
            conn.close()


engine = MarketEngine(
    MarketConfig(
        candle_seconds=1,            # 1s no DB (frontend agrega para 5m)
        tick_seconds=1.0,
        start_price=100.0,
        initial_usd_liquidity=2_000_000.0,


        seed_enabled=True,
        seed_seconds=7 * 24 * 60 * 60,      # 1 semana
        seed_candle_seconds=60,             # seed leve (1m)
        seed_step_pct=0.0007,               # ajustável

        fee_rate=0.0,
        min_equity=0.0,
        leverage_max=3.0,
        stopout_equity=0.0,
    )
)
