function $(id) {
  const el = document.getElementById(id);
  if (!el) console.warn(`Elemento #${id} não encontrado`);
  return el;
}

// ===== Elements =====
const elStatus = $("status");
const elPrice  = $("price");

const elMeNick   = $("meNick");
const elMeCode   = $("meCode");
const elMeCash   = $("meCash");
const elMePos    = $("mePos");
const elMeAvg    = $("meAvgPrice");
const elMeEquity = $("meEquity");

const elPnlUnr = $("mePnlUnrealized");
const elPnlRel = $("mePnlRealized");
const elPnlTot = $("mePnlTotal");

const elPosState = $("posState");
const elPosSize  = $("posSize");
const btnCloseAll = $("btnCloseAll");

const elQty = $("qty");
const btnReload = $("reload");
const btnBuy = $("btnBuy");
const btnSell = $("btnSell");
const btnResetMe = $("btnResetMe");

const joinModal = $("joinModal");
const joinCode  = $("joinCode");
const joinNick  = $("joinNick");
const btnCloseJoin = $("btnCloseJoin");

const elLeaderboard = $("leaderboard");
const elLeaderboardEmpty = $("leaderboardEmpty");

const elTradesBuys = $("tradesBuys");
const elTradesSells = $("tradesSells");
const elTradesBuysEmpty = $("tradesBuysEmpty");
const elTradesSellsEmpty = $("tradesSellsEmpty");
const elBuysCount = $("buysCount");
const elSellsCount = $("sellsCount");

const phasePill = $("phasePill");

// ===== Config =====
const TF_SECONDS = 300;          // 5 minutos
const CANDLES_LIMIT = 300;       // ~25h
const LIVE_POLL_MS = 1000;

// ===== Helpers =====
function setStatus(t){ if(elStatus) elStatus.textContent=t; }
function setPriceTxt(t){ if(elPrice) elPrice.textContent=t; }

const fmtMoney = new Intl.NumberFormat("pt-PT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtQty   = new Intl.NumberFormat("pt-PT", { minimumFractionDigits: 5, maximumFractionDigits: 5 });
const fmtPx    = new Intl.NumberFormat("pt-PT", { minimumFractionDigits: 5, maximumFractionDigits: 5 });

function money(n){ return fmtMoney.format(Number(n)||0); }
function qty(n){ return fmtQty.format(Number(n)||0); }
function px(n){ return fmtPx.format(Number(n)||0); }

function signMoney(n){
  const v = Number(n)||0;
  const s = v >= 0 ? "+" : "-";
  return `${s}${money(Math.abs(v))}`;
}

function applyPnlStyle(el, v) {
  if (!el) return;
  el.style.color = "";
  const n = Number(v);
  if (!Number.isFinite(n) || Math.abs(n) < 1e-9) return;
  el.style.color = n > 0 ? "var(--green)" : "var(--red)";
}

function stripHtml(s){ return String(s).replace(/<[^>]*>/g, ""); }

function toastOk(title, html) {
  if (window.Swal) {
    return Swal.fire({ icon:"success", title, html, timer: 2200, timerProgressBar:true, showConfirmButton:false });
  }
  alert(title + (html ? `\n${stripHtml(html)}` : ""));
}

function toastErr(title, html) {
  if (window.Swal) {
    return Swal.fire({ icon:"error", title, html, confirmButtonText:"OK" });
  }
  alert(title + (html ? `\n${stripHtml(html)}` : ""));
}

function confirmBox(title, text) {
  if (window.Swal) {
    return Swal.fire({
      icon:"warning", title, text,
      showCancelButton:true, confirmButtonText:"Sim", cancelButtonText:"Não"
    }).then(r => r.isConfirmed);
  }
  return Promise.resolve(confirm(text || title));
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function getMe(){
  return {
    code: (localStorage.getItem("arena_code") || "").trim(),
    nick: (localStorage.getItem("arena_nick") || "").trim(),
  };
}
function setMe(code,nick){
  localStorage.setItem("arena_code", code.trim());
  localStorage.setItem("arena_nick", nick.trim());
}
function clearMe(){
  localStorage.removeItem("arena_code");
  localStorage.removeItem("arena_nick");
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    cache:"no-store",
    headers:{ "Content-Type":"application/json" },
    ...opts,
  });

  let data = null;
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("application/json")) data = await r.json().catch(() => null);
  else data = await r.text().catch(() => null);

  if (!r.ok) {
    const msg = (data && data.detail) ? data.detail : `Erro HTTP ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

// ===== Modal =====
function openModal(){
  if(!joinModal) return;
  joinModal.classList.add("active");
  joinModal.setAttribute("aria-hidden","false");
  const me = getMe();
  if(joinCode) joinCode.value = me.code || "";
  if(joinNick) joinNick.value = me.nick || "";
  setTimeout(() => {
    if (joinCode && !joinCode.value) joinCode.focus();
    else if (joinNick) joinNick.focus();
  }, 60);
}
function closeModal(){
  if(!joinModal) return;
  joinModal.classList.remove("active");
  joinModal.setAttribute("aria-hidden","true");
}

async function doJoin(){
  try{
    const code = (joinCode?.value || "").trim();
    const nick = (joinNick?.value || "").trim();
    if(!code || !nick){
      await toastErr("Campos obrigatórios", "Preencha <b>código</b> e <b>nick</b>.");
      return;
    }
    await api("/api/join", { method:"POST", body: JSON.stringify({code,nick}) });
    setMe(code,nick);
    closeModal();
    renderMeIdentity();
    await refreshAll();
    await toastOk("Entrou na Arena", `Nick: <b>${escapeHtml(nick)}</b><br/>Saldo inicial: <b>10.000,00</b>`);
  }catch(e){
    console.error(e);
    await toastErr("Falha ao entrar", escapeHtml(e.message || "Erro"));
  }
}

// ===== Chart =====
const chart = LightweightCharts.createChart(document.getElementById("chart"), {
  autoSize:true,
  layout:{ background:{ color:"#0b0f14" }, textColor:"#b6c2cf" },
  grid:{ vertLines:{ color:"#111826" }, horzLines:{ color:"#111826" } },
  timeScale:{ timeVisible:true, secondsVisible:false },
});

let series = null;
if (typeof chart.addCandlestickSeries === "function") series = chart.addCandlestickSeries();
else if (typeof chart.addSeries === "function") series = chart.addSeries(LightweightCharts.CandlestickSeries);
else throw new Error("LightweightCharts incompatível.");

let lastCandleTime = 0;

async function ensureMarketStarted(){
  try{
    const st = await api("/api/state");
    if (st && st.started) {
      if (phasePill) {
        phasePill.textContent = "LIVE";
        phasePill.classList.add("is-live");
      }
      return;
    }
  }catch(_e){}

  try{
    const snap = await api("/api/start", { method:"POST" });
    if (snap && snap.started) {
      if (phasePill) {
        phasePill.textContent = "LIVE";
        phasePill.classList.add("is-live");
      }
      setStatus("mercado iniciado");
    }
  }catch(e){
    console.error(e);
    setStatus("falha ao iniciar");
  }
}

async function loadCandles(){
  setStatus("carregando…");
  const data = await api(`/api/candles?limit=${encodeURIComponent(CANDLES_LIMIT)}&tf=${encodeURIComponent(TF_SECONDS)}`);
  if(!Array.isArray(data) || data.length === 0){ setStatus("sem dados"); return; }
  series.setData(data);
  setStatus("ok");

  const last = data[data.length-1];
  lastCandleTime = last.time;
  setPriceTxt(px(last.close));
}

async function pollLiveCandle(){
  try{
    const data = await api(`/api/candles?limit=2&tf=${encodeURIComponent(TF_SECONDS)}`);
    if(!Array.isArray(data) || data.length === 0) return;

    const last = data[data.length-1];
    series.update(last);

    if(last.time !== lastCandleTime){
      lastCandleTime = last.time;
      await loadCandles(); // garante consistência quando vira candle
      return;
    }

    setPriceTxt(px(last.close));
  }catch(_e){}
}

// ===== UI Render =====
function renderMeIdentity(){
  const me = getMe();
  if(elMeNick) elMeNick.textContent = me.nick || "—";
  if(elMeCode) elMeCode.textContent = me.code || "—";
}

function renderPosition(pos){
  const p = Number(pos)||0;
  if (Math.abs(p) < 1e-12) return { label: "FLAT", size: 0, dir: "FLAT" };
  if (p > 0) return { label: "LONG", size: p, dir: "LONG" };
  return { label: "SHORT", size: Math.abs(p), dir: "SHORT" };
}

function renderMeData(d){
  const cash = Number(d.cash)||0;
  const equity = Number(d.equity)||0;
  const avg = Number(d.avg_price)||0;

  if(elMeCash) elMeCash.textContent = money(cash);
  if(elMeEquity) elMeEquity.textContent = money(equity);

  const p = renderPosition(d.pos);
  if(elMePos) elMePos.textContent = p.label;

  if(elPosState) elPosState.textContent = p.dir === "FLAT" ? "Sem posição" : p.label;
  if(elPosSize)  elPosSize.textContent  = p.dir === "FLAT" ? "0,00000" : qty(p.size);

  if(elMeAvg) elMeAvg.textContent = (avg > 0 ? px(avg) : "—");

  if(elPnlUnr) elPnlUnr.textContent = signMoney(d.pnl_unrealized);
  if(elPnlRel) elPnlRel.textContent = signMoney(d.pnl_realized);
  if(elPnlTot) elPnlTot.textContent = signMoney(d.pnl_total);

  applyPnlStyle(elPnlUnr, d.pnl_unrealized);
  applyPnlStyle(elPnlRel, d.pnl_realized);
  applyPnlStyle(elPnlTot, d.pnl_total);

  // Fechar posição só desabilita se FLAT
  if (btnCloseAll) btnCloseAll.disabled = (p.dir === "FLAT");
}

async function refreshMe(){
  const me = getMe();
  renderMeIdentity();

  if(!me.code){
    renderMeData({
      cash:0, equity:0, pos:0, avg_price:0,
      pnl_unrealized:0, pnl_realized:0, pnl_total:0
    });
    return;
  }

  const data = await api(`/api/me?code=${encodeURIComponent(me.code)}`);
  renderMeData(data);
}

async function refreshLeaderboard(){
  if(!elLeaderboard) return;

  const rows = await api("/api/leaderboard?limit=50");
  elLeaderboard.innerHTML = "";

  if(!Array.isArray(rows) || rows.length === 0){
    if(elLeaderboardEmpty){
      elLeaderboard.appendChild(elLeaderboardEmpty);
      elLeaderboardEmpty.style.display = "block";
    }
    return;
  }
  if(elLeaderboardEmpty) elLeaderboardEmpty.style.display = "none";

  rows.slice(0,15).forEach((r,idx) => {
    const div = document.createElement("div");
    div.className = "lb-row";
    const pnl = Number(r.pnl)||0;
    div.innerHTML = `
      <span>${idx+1}. ${escapeHtml(r.nick)}</span>
      <span>${money(r.equity)} | ${signMoney(pnl)}</span>
    `;
    if(pnl > 0) div.style.color = "var(--green)";
    else if(pnl < 0) div.style.color = "var(--red)";
    elLeaderboard.appendChild(div);
  });
}

async function refreshHistorySplit(){
  const me = getMe();

  if (elTradesBuys) elTradesBuys.innerHTML = "";
  if (elTradesSells) elTradesSells.innerHTML = "";
  if (elBuysCount) elBuysCount.textContent = "—";
  if (elSellsCount) elSellsCount.textContent = "—";

  if(!me.code){
    if(elTradesBuys && elTradesBuysEmpty){ elTradesBuys.appendChild(elTradesBuysEmpty); elTradesBuysEmpty.style.display="block"; }
    if(elTradesSells && elTradesSellsEmpty){ elTradesSells.appendChild(elTradesSellsEmpty); elTradesSellsEmpty.style.display="block"; }
    return;
  }

  const rows = await api(`/api/trades?code=${encodeURIComponent(me.code)}&limit=80`);
  const buys = [];
  const sells = [];

  (rows || []).forEach(t => {
    const s = String(t.side).toUpperCase();
    if (s === "BUY") buys.push(t);
    else if (s === "SELL") sells.push(t);
  });

  if(elBuysCount) elBuysCount.textContent = String(buys.length);
  if(elSellsCount) elSellsCount.textContent = String(sells.length);

  // BUY list
  if (!buys.length) {
    if(elTradesBuys && elTradesBuysEmpty){ elTradesBuys.appendChild(elTradesBuysEmpty); elTradesBuysEmpty.style.display="block"; }
  } else {
    if(elTradesBuysEmpty) elTradesBuysEmpty.style.display="none";
    buys.slice(-20).reverse().forEach(t => {
      const div = document.createElement("div");
      div.className = "trade-row";
      div.innerHTML = `<span>BUY</span><span>${money(t.notional)}$ @ ${px(t.price)}</span>`;
      elTradesBuys?.appendChild(div);
    });
  }

  // SELL list
  if (!sells.length) {
    if(elTradesSells && elTradesSellsEmpty){ elTradesSells.appendChild(elTradesSellsEmpty); elTradesSellsEmpty.style.display="block"; }
  } else {
    if(elTradesSellsEmpty) elTradesSellsEmpty.style.display="none";
    sells.slice(-20).reverse().forEach(t => {
      const div = document.createElement("div");
      div.className = "trade-row";
      div.innerHTML = `<span>SELL</span><span>${money(t.notional)}$ @ ${px(t.price)}</span>`;
      elTradesSells?.appendChild(div);
    });
  }
}

// ===== Trading =====
function parseUsdInput(){
  const v = (elQty?.value || "").replace(",", ".").trim();
  const n = Number(v);
  if(!Number.isFinite(n) || n <= 0) return null;
  return n;
}

async function doTrade(side){
  try{
    const me = getMe();
    if(!me.code){
      openModal();
      await toastErr("Sem jogador", "Entre com <b>código</b> e <b>nick</b> primeiro.");
      return;
    }

    const usd = parseUsdInput();
    if(usd === null){
      await toastErr("Quantidade inválida", "Use valor em USD (ex: 10, 50, 100).");
      return;
    }

    const res = await api("/api/trade", { method:"POST", body: JSON.stringify({ code: me.code, side, usd }) });

    // backend devolve res.me
    if (res && res.me) renderMeData(res.me);

    await Promise.allSettled([refreshLeaderboard(), refreshHistorySplit()]);

    const p = renderPosition(res.me?.pos ?? 0);

    await toastOk(
      side === "BUY" ? "BUY executado" : "SELL executado",
      `
      Valor: <b>${money(usd)}$</b><br/>
      Preço após: <b>${px(res.price_after)}</b><br/>
      ${res.rich_out ? `RICH recebido: <b>${qty(res.rich_out)}</b><br/>` : ""}
      ${res.usd_out ? `USD recebido: <b>${money(res.usd_out)}$</b><br/>` : ""}
      Posição: <b>${p.label} ${p.dir === "FLAT" ? "" : qty(p.size)}</b><br/>
      Preço médio: <b>${res.me?.avg_price ? px(res.me.avg_price) : "—"}</b><br/>
      PnL Total: <b>${signMoney(res.me?.pnl_total || 0)}</b><br/>
      Património: <b>${money(res.me?.equity || 0)}</b>
      `
    );
  }catch(e){
    console.error(e);
    await toastErr("Trade recusado", escapeHtml(e.message || "Erro"));
  }
}

async function closePositionAll(){
  try{
    const me = getMe();
    if(!me.code){ openModal(); return; }

    const m = await api(`/api/me?code=${encodeURIComponent(me.code)}`);
    const pos = Number(m.pos)||0;

    if (Math.abs(pos) < 1e-12) {
      await toastErr("Nada para fechar", "Você está FLAT.");
      return;
    }

    const p = renderPosition(pos);
    const ok = await confirmBox("Fechar posição", `Deseja fechar 100% da posição (${p.label}) agora?`);
    if(!ok) return;

    // 🔥 Fechar correto:
    // LONG -> SELL usd equivalente
    // SHORT -> BUY  usd equivalente
    const usdToClose = Math.abs(pos) * Number(m.price);
    const sideToClose = pos > 0 ? "SELL" : "BUY";

    await api("/api/trade", {
      method: "POST",
      body: JSON.stringify({ code: me.code, side: sideToClose, usd: usdToClose })
    });

    await refreshAll();
    await toastOk("Posição fechada", "Você está FLAT. O resultado foi para o histórico.");
  } catch (e) {
    console.error(e);
    await toastErr("Falha ao fechar", escapeHtml(e.message || "Erro"));
  }
}

// ===== Refresh all =====
async function refreshAll(){
  await Promise.allSettled([
    refreshMe(),
    refreshLeaderboard(),
    refreshHistorySplit()
  ]);
}

// ===== Delegation / bindings =====
function setupDelegation(){
  document.addEventListener("click", async (e) => {
    const t = e.target;

    if (t && (t.id === "btnOpenJoin" || t.id === "phasePill")) { openModal(); return; }
    if (t && t.id === "btnCloseJoin") { closeModal(); return; }
    if (t === joinModal) { closeModal(); return; }
    if (t && t.id === "btnJoin") { await doJoin(); return; }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
    if (e.key === "Enter" && joinModal?.classList.contains("active")) {
      if (document.activeElement === joinCode) joinNick?.focus();
      else doJoin();
    }
  });
}

function bindOtherEvents(){
  btnReload?.addEventListener("click", () => loadCandles());
  btnBuy?.addEventListener("click", () => doTrade("BUY"));
  btnSell?.addEventListener("click", () => doTrade("SELL"));
  btnCloseAll?.addEventListener("click", () => closePositionAll());

  btnResetMe?.addEventListener("click", async () => {
    const ok = await confirmBox("Trocar jogador", "Deseja limpar o jogador atual neste navegador?");
    if(!ok) return;
    clearMe();
    renderMeIdentity();
    await refreshAll();
    await toastOk("Jogador limpo", "Agora podes entrar com outro código.");
  });

  elQty?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doTrade("BUY");
  });
}

// ===== Init =====
(async () => {
  setupDelegation();
  bindOtherEvents();

  await ensureMarketStarted();

  renderMeIdentity();
  await loadCandles();
  await refreshAll();

  setInterval(pollLiveCandle, LIVE_POLL_MS);
  setInterval(refreshLeaderboard, 2000);
  setInterval(refreshMe, 1000);
  setInterval(refreshHistorySplit, 2000);
})();
