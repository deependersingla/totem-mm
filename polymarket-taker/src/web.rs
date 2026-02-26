pub const INDEX_HTML: &str = r##"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Totem Taker</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;padding:16px}
h1{font-size:20px;margin-bottom:12px;color:#58a6ff}
h2{font-size:14px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:900px;margin:0 auto}
.full{grid-column:1/-1}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
.status-bar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
.badge{padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;text-transform:uppercase}
.badge-idle{background:#30363d;color:#8b949e}
.badge-running{background:#238636;color:#fff}
.badge-paused{background:#d29922;color:#000}
.badge-over{background:#da3633;color:#fff}
.badge-dry{background:#6e40c9;color:#fff}
.stat{margin:4px 0}
.stat span{color:#8b949e;font-size:12px}
.stat strong{color:#e1e4e8;font-size:14px;margin-left:4px}
input,select{background:#0d1117;border:1px solid #30363d;color:#e1e4e8;padding:6px 10px;border-radius:4px;font-size:13px;width:100%}
input:focus,select:focus{outline:none;border-color:#58a6ff}
label{font-size:12px;color:#8b949e;display:block;margin-bottom:3px;margin-top:8px}
.row{display:flex;gap:8px}
.row>*{flex:1}
button{padding:8px 14px;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.85}
button:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:#238636;color:#fff}
.btn-warn{background:#d29922;color:#000}
.btn-danger{background:#da3633;color:#fff}
.btn-signal{background:#30363d;color:#e1e4e8;min-width:42px;font-size:15px;padding:10px 8px}
.btn-signal.wicket{background:#da3633;color:#fff}
.signal-grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.events{max-height:280px;overflow-y:auto;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace}
.events::-webkit-scrollbar{width:6px}
.events::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
.ev{padding:3px 0;border-bottom:1px solid #21262d;display:flex;gap:8px}
.ev-ts{color:#484f58;min-width:55px}
.ev-kind{color:#58a6ff;min-width:60px;font-weight:600}
.ev-detail{color:#c9d1d9}
.ev-error .ev-kind{color:#da3633}
.ev-warn .ev-kind{color:#d29922}
.ev-trade .ev-kind{color:#3fb950}
.ev-wicket .ev-kind{color:#f85149}
.book-row{display:flex;justify-content:space-between;font-size:13px;padding:2px 0}
.book-bid{color:#3fb950}
.book-ask{color:#f85149}
.locked-notice{font-size:11px;color:#d29922;margin-top:4px}
</style>
</head>
<body>

<div class="grid">

<div class="card full">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h1>Totem Taker</h1>
    <div class="status-bar">
      <span id="phaseBadge" class="badge badge-idle">IDLE</span>
      <span id="dryBadge" class="badge badge-dry" style="display:none">DRY RUN</span>
    </div>
  </div>
</div>

<!-- Position -->
<div class="card">
  <h2>Position</h2>
  <div class="stat"><span id="teamALabel">TEAM_A</span> <strong id="teamATokens">0</strong></div>
  <div class="stat"><span id="teamBLabel">TEAM_B</span> <strong id="teamBTokens">0</strong></div>
  <div class="stat"><span>Spent</span> <strong id="spent">0</strong> / <strong id="budget">100</strong></div>
  <div class="stat"><span>Remaining</span> <strong id="remaining">100</strong></div>
  <div class="stat"><span>Trades</span> <strong id="trades">0</strong></div>
  <div class="stat"><span>Live Orders</span> <strong id="liveOrders">0</strong></div>
</div>

<!-- Book -->
<div class="card">
  <h2>Order Book</h2>
  <div style="margin-bottom:8px">
    <div style="font-size:12px;color:#8b949e" id="bookALabel">Team A</div>
    <div class="book-row"><span class="book-bid" id="aBid">—</span><span class="book-ask" id="aAsk">—</span></div>
  </div>
  <div>
    <div style="font-size:12px;color:#8b949e" id="bookBLabel">Team B</div>
    <div class="book-row"><span class="book-bid" id="bBid">—</span><span class="book-ask" id="bAsk">—</span></div>
  </div>
  <div style="margin-top:10px">
    <div class="stat"><span>Batting</span> <strong id="batting">—</strong></div>
    <div class="stat"><span>Bowling</span> <strong id="bowling">—</strong></div>
    <div class="stat"><span>Innings</span> <strong id="innings">1</strong></div>
  </div>
</div>

<!-- Inventory Chart -->
<div class="card full">
  <h2>Inventory</h2>
  <canvas id="invChart" width="860" height="180" style="width:100%;height:180px;border-radius:4px"></canvas>
  <div style="font-size:11px;color:#8b949e;margin-top:4px;display:flex;gap:16px">
    <span><span style="color:#58a6ff">&#9632;</span> Team A</span>
    <span><span style="color:#f0883e">&#9632;</span> Team B</span>
  </div>
</div>

<!-- Setup -->
<div class="card">
  <h2>Match Setup</h2>
  <div id="setupLock">
    <div class="row">
      <div><label>Team A</label><input id="sTeamA" value=""></div>
      <div><label>Team B</label><input id="sTeamB" value=""></div>
    </div>
    <label>Team A Token ID</label><input id="sTokenA" value="">
    <label>Team B Token ID</label><input id="sTokenB" value="">
    <label>Condition ID</label><input id="sCondition" value="" placeholder="0x... (for CTF split/merge)">
    <div class="row">
      <div><label>First Batting</label>
        <select id="sBatFirst"><option value="A">A</option><option value="B">B</option></select>
      </div>
      <div><label>Neg Risk</label>
        <select id="sNegRisk"><option value="false">No</option><option value="true">Yes</option></select>
      </div>
    </div>
    <button class="btn-primary" style="margin-top:10px;width:100%" onclick="saveSetup()">Save Setup</button>
  </div>
  <div id="setupLockedMsg" class="locked-notice" style="display:none">Setup locked while match is running</div>
</div>

<!-- Wallet -->
<div class="card">
  <h2>Wallet</h2>
  <div id="walletLock">
    <label>Private Key</label><input id="wKey" type="password" placeholder="0x...">
    <label>Address</label><input id="wAddr" placeholder="0x...">
    <div class="row">
      <div><label>Sig Type</label>
        <select id="wSigType"><option value="0">EOA (0)</option><option value="1">Proxy (1)</option></select>
      </div>
    </div>
    <button class="btn-primary" style="margin-top:10px;width:100%" onclick="saveWallet()">Save Wallet</button>
  </div>
  <div id="walletLockedMsg" class="locked-notice" style="display:none">Wallet locked while match is running</div>
  <div class="stat" style="margin-top:6px"><span>Status</span> <strong id="walletStatus">Not Set</strong></div>
</div>

<!-- Limits -->
<div class="card">
  <h2>Limits</h2>
  <div class="row">
    <div><label>Budget ($)</label><input id="lBudget" type="number" value="100"></div>
  </div>
  <div class="row">
    <div><label>Max Trade ($)</label><input id="lMaxTrade" type="number" value="10"></div>
    <div><label>Revert Delay (ms)</label><input id="lDelay" type="number" value="3000"></div>
  </div>
  <div class="row">
    <div><label>Dry Run</label>
      <select id="lDryRun"><option value="true">Yes</option><option value="false">No</option></select>
    </div>
  </div>
  <button class="btn-primary" style="margin-top:10px;width:100%" onclick="saveLimits()">Save Limits</button>
</div>

<!-- Controls -->
<div class="card">
  <h2>Match Controls</h2>
  <div class="row" style="margin-bottom:8px">
    <button id="btnStart" class="btn-primary" onclick="startInnings()">Start Innings</button>
    <button id="btnStop" class="btn-warn" onclick="stopInnings()" disabled>Stop Innings</button>
  </div>
  <div class="row">
    <button id="btnMO" class="btn-danger" onclick="matchOver()">Match Over</button>
    <button class="btn-danger" onclick="cancelAll()">Cancel All Orders</button>
  </div>
  <button class="btn-warn" style="margin-top:8px;width:100%" onclick="resetMatch()">Reset (New Match)</button>
</div>

<!-- CTF Split / Merge / Redeem -->
<div class="card full">
  <h2>CTF On-Chain</h2>
  <div class="row">
    <div style="flex:2">
      <label>Amount (USDC / tokens)</label>
      <input id="ctfAmount" type="number" value="10" min="1" step="1">
    </div>
    <div style="flex:3;display:flex;gap:8px;align-items:flex-end">
      <button class="btn-primary" style="flex:1" onclick="ctfSplit()">Split USDC → Tokens</button>
      <button class="btn-warn" style="flex:1" onclick="ctfMerge()">Merge Tokens → USDC</button>
      <button class="btn-danger" style="flex:1" onclick="ctfRedeem()">Redeem (Post-Resolve)</button>
    </div>
  </div>
  <div style="font-size:11px;color:#8b949e;margin-top:6px">
    <b>Split:</b> $X USDC → X YES + X NO tokens &nbsp;|&nbsp;
    <b>Merge:</b> X YES + X NO → $X USDC &nbsp;|&nbsp;
    <b>Redeem:</b> winning tokens → USDC (after market resolves)
  </div>
</div>

<!-- Signals -->
<div class="card full">
  <h2>Signals</h2>
  <div class="signal-grid">
    <button class="btn-signal" onclick="sendSignal('0')">0</button>
    <button class="btn-signal" onclick="sendSignal('1')">1</button>
    <button class="btn-signal" onclick="sendSignal('2')">2</button>
    <button class="btn-signal" onclick="sendSignal('3')">3</button>
    <button class="btn-signal" onclick="sendSignal('4')">4</button>
    <button class="btn-signal" onclick="sendSignal('5')">5</button>
    <button class="btn-signal" onclick="sendSignal('6')">6</button>
    <button class="btn-signal wicket" onclick="sendSignal('W')">W</button>
    <button class="btn-signal" onclick="sendSignal('Wd')">Wd</button>
    <button class="btn-signal" onclick="sendSignal('N')">N</button>
  </div>
</div>

<!-- Event Log -->
<div class="card full">
  <h2>Event Log</h2>
  <div class="events" id="eventLog"></div>
</div>

</div>

<script>
const API = '';
let pollTimer = null;

async function api(path, opts) {
  try {
    const r = await fetch(API + path, opts);
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j || r.statusText);
    return j;
  } catch(e) {
    showToast(e.message);
    throw e;
  }
}

function showToast(msg) {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;top:16px;right:16px;background:#da3633;color:#fff;padding:10px 16px;border-radius:6px;font-size:13px;z-index:999;max-width:350px';
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 4000);
}

async function pollStatus() {
  try {
    const s = await api('/api/status');
    const el = id => document.getElementById(id);

    // phase badge
    const pb = el('phaseBadge');
    pb.textContent = s.phase.replace('_',' ').toUpperCase();
    pb.className = 'badge badge-' + ({idle:'idle',innings_running:'running',innings_paused:'paused',match_over:'over'}[s.phase]||'idle');

    const db = el('dryBadge');
    db.style.display = s.dry_run ? '' : 'none';

    el('teamALabel').textContent = s.team_a_name;
    el('teamBLabel').textContent = s.team_b_name;
    el('teamATokens').textContent = s.team_a_tokens;
    el('teamBTokens').textContent = s.team_b_tokens;
    el('spent').textContent = s.total_spent;
    el('budget').textContent = s.total_budget;
    el('remaining').textContent = s.remaining;
    el('trades').textContent = s.trade_count;
    el('liveOrders').textContent = s.live_orders;

    el('bookALabel').textContent = s.team_a_name;
    el('bookBLabel').textContent = s.team_b_name;
    el('aBid').textContent = s.book_a_bid != null ? s.book_a_bid+'¢' : '—';
    el('aAsk').textContent = s.book_a_ask != null ? s.book_a_ask+'¢' : '—';
    el('bBid').textContent = s.book_b_bid != null ? s.book_b_bid+'¢' : '—';
    el('bAsk').textContent = s.book_b_ask != null ? s.book_b_ask+'¢' : '—';

    el('batting').textContent = s.batting;
    el('bowling').textContent = s.bowling;
    el('innings').textContent = s.innings;
    el('walletStatus').textContent = s.wallet_set ? 'Configured' : 'Not Set';
    el('walletStatus').style.color = s.wallet_set ? '#3fb950' : '#da3633';

    const running = s.phase === 'innings_running';
    el('btnStart').disabled = running;
    el('btnStop').disabled = !running;

    // lock setup + wallet while running
    el('setupLock').style.display = running ? 'none' : '';
    el('setupLockedMsg').style.display = running ? '' : 'none';
    el('walletLock').style.display = running ? 'none' : '';
    el('walletLockedMsg').style.display = running ? '' : 'none';

    // disable signal buttons when not running
    document.querySelectorAll('.btn-signal').forEach(b => b.disabled = !running);

  } catch(e) { /* ignore poll errors */ }
}

async function pollEvents() {
  try {
    const events = await api('/api/events');
    const el = document.getElementById('eventLog');
    el.innerHTML = events.map(e => {
      let cls = 'ev';
      if (e.kind === 'error') cls += ' ev-error';
      else if (e.kind === 'warn') cls += ' ev-warn';
      else if (e.kind === 'trade') cls += ' ev-trade';
      else if (e.kind === 'wicket') cls += ' ev-wicket';
      return `<div class="${cls}"><span class="ev-ts">${e.ts}</span><span class="ev-kind">${e.kind}</span><span class="ev-detail">${e.detail}</span></div>`;
    }).reverse().join('');
  } catch(e) {}
}

async function loadConfig() {
  try {
    const c = await api('/api/config');
    document.getElementById('sTeamA').value = c.team_a_name;
    document.getElementById('sTeamB').value = c.team_b_name;
    document.getElementById('sTokenA').value = c.team_a_token_id;
    document.getElementById('sTokenB').value = c.team_b_token_id;
    document.getElementById('sCondition').value = c.condition_id || '';
    document.getElementById('sBatFirst').value = c.first_batting === 'TEAM_B' ? 'B' : 'A';
    document.getElementById('sNegRisk').value = String(c.neg_risk);
    document.getElementById('lBudget').value = c.total_budget_usdc;
    document.getElementById('lMaxTrade').value = c.max_trade_usdc;
    document.getElementById('lDelay').value = c.revert_delay_ms;
    document.getElementById('lDryRun').value = String(c.dry_run);
    document.getElementById('wSigType').value = c.signature_type;
    if (c.polymarket_address) document.getElementById('wAddr').value = c.polymarket_address;
    const wk = document.getElementById('wKey');
    if (c.private_key_set) {
      wk.placeholder = '********** (already set)';
    }
  } catch(e) {}
}

async function saveSetup() {
  await api('/api/setup', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
    team_a_name: document.getElementById('sTeamA').value,
    team_b_name: document.getElementById('sTeamB').value,
    team_a_token_id: document.getElementById('sTokenA').value,
    team_b_token_id: document.getElementById('sTokenB').value,
    condition_id: document.getElementById('sCondition').value,
    first_batting: document.getElementById('sBatFirst').value,
    neg_risk: document.getElementById('sNegRisk').value === 'true',
  })});
}

async function saveWallet() {
  const key = document.getElementById('wKey').value;
  const addr = document.getElementById('wAddr').value;
  const sig = parseInt(document.getElementById('wSigType').value);
  const body = {};
  if (key) body.private_key = key;
  if (addr) body.address = addr;
  body.signature_type = sig;
  await api('/api/wallet', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
}

async function saveLimits() {
  await api('/api/limits', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
    total_budget_usdc: document.getElementById('lBudget').value,
    max_trade_usdc: document.getElementById('lMaxTrade').value,
    revert_delay_ms: parseInt(document.getElementById('lDelay').value),
    dry_run: document.getElementById('lDryRun').value === 'true',
  })});
}

async function startInnings() { await api('/api/start-innings', {method:'POST'}); }
async function stopInnings() { await api('/api/stop-innings', {method:'POST'}); }
async function matchOver() {
  if (!confirm('End the match?')) return;
  await api('/api/match-over', {method:'POST'});
}
async function cancelAll() { await api('/api/cancel-all', {method:'POST'}); }
async function resetMatch() {
  if (!confirm('Reset everything for a new match?')) return;
  await api('/api/reset', {method:'POST'});
  loadConfig();
}
async function sendSignal(sig) { await api('/api/signal', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({signal:sig})}); }

async function ctfSplit() {
  const amt = parseInt(document.getElementById('ctfAmount').value);
  if (!amt || amt <= 0) { showToast('enter a positive amount'); return; }
  if (!confirm('Split $' + amt + ' USDC into ' + amt + ' YES + ' + amt + ' NO tokens?')) return;
  await api('/api/ctf-split', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount_usdc: amt})});
}
async function ctfMerge() {
  const amt = parseInt(document.getElementById('ctfAmount').value);
  if (!amt || amt <= 0) { showToast('enter a positive amount'); return; }
  if (!confirm('Merge ' + amt + ' YES + ' + amt + ' NO tokens back into $' + amt + ' USDC?')) return;
  await api('/api/ctf-merge', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({amount_tokens: amt})});
}
async function ctfRedeem() {
  if (!confirm('Redeem all winning tokens for USDC? (market must be resolved)')) return;
  await api('/api/ctf-redeem', {method:'POST'});
}

function drawInventoryChart(data) {
  const canvas = document.getElementById('invChart');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);

  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, w, h);

  if (!data || data.length === 0) {
    ctx.fillStyle = '#484f58';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No inventory data yet', w/2, h/2);
    return;
  }

  const pad = {top:10, right:10, bottom:22, left:44};
  const cw = w - pad.left - pad.right;
  const ch = h - pad.top - pad.bottom;

  const allVals = data.flatMap(d => [parseFloat(d.team_a), parseFloat(d.team_b)]);
  const maxVal = Math.max(...allVals, 1);
  const minVal = Math.min(...allVals, 0);
  const range = maxVal - minVal || 1;

  const xStep = data.length > 1 ? cw / (data.length - 1) : cw;

  function drawLine(key, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    data.forEach((d, i) => {
      const x = pad.left + (data.length > 1 ? i * xStep : cw/2);
      const y = pad.top + ch - ((parseFloat(d[key]) - minVal) / range) * ch;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    if (data.length === 1) {
      ctx.fillStyle = color;
      ctx.beginPath();
      const x = pad.left + cw/2;
      const y = pad.top + ch - ((parseFloat(data[0][key]) - minVal) / range) * ch;
      ctx.arc(x, y, 3, 0, Math.PI*2);
      ctx.fill();
    }
  }

  // grid lines
  ctx.strokeStyle = '#21262d';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (ch / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(w - pad.right, y); ctx.stroke();
    const val = maxVal - (range / 4) * i;
    ctx.fillStyle = '#484f58';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(val.toFixed(0), pad.left - 4, y + 3);
  }

  drawLine('team_a', '#58a6ff');
  drawLine('team_b', '#f0883e');

  // x-axis labels (show a few timestamps)
  ctx.fillStyle = '#484f58';
  ctx.font = '9px sans-serif';
  ctx.textAlign = 'center';
  const labelCount = Math.min(data.length, 8);
  const labelStep = Math.max(1, Math.floor(data.length / labelCount));
  for (let i = 0; i < data.length; i += labelStep) {
    const x = pad.left + (data.length > 1 ? i * xStep : cw/2);
    ctx.fillText(data[i].ts, x, h - 4);
  }
  if (data.length > 1) {
    const x = pad.left + (data.length - 1) * xStep;
    ctx.fillText(data[data.length-1].ts, x, h - 4);
  }
}

async function pollInventory() {
  try {
    const data = await api('/api/inventory');
    drawInventoryChart(data);
  } catch(e) {}
}

loadConfig();
pollStatus();
pollEvents();
pollInventory();
setInterval(() => { pollStatus(); pollEvents(); pollInventory(); }, 1500);
</script>
</body>
</html>
"##;
