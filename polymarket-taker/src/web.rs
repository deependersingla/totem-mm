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
    <div><label>Initial Buy ($)</label><input id="lInitial" type="number" value="20"></div>
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
    document.getElementById('sBatFirst').value = c.first_batting === 'TEAM_B' ? 'B' : 'A';
    document.getElementById('sNegRisk').value = String(c.neg_risk);
    document.getElementById('lBudget').value = c.total_budget_usdc;
    document.getElementById('lInitial').value = c.initial_buy_usdc;
    document.getElementById('lMaxTrade').value = c.max_trade_usdc;
    document.getElementById('lDelay').value = c.revert_delay_ms;
    document.getElementById('lDryRun').value = String(c.dry_run);
    document.getElementById('wSigType').value = c.signature_type;
    if (c.polymarket_address) document.getElementById('wAddr').value = c.polymarket_address;
  } catch(e) {}
}

async function saveSetup() {
  await api('/api/setup', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
    team_a_name: document.getElementById('sTeamA').value,
    team_b_name: document.getElementById('sTeamB').value,
    team_a_token_id: document.getElementById('sTokenA').value,
    team_b_token_id: document.getElementById('sTokenB').value,
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
    initial_buy_usdc: document.getElementById('lInitial').value,
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

loadConfig();
pollStatus();
pollEvents();
setInterval(() => { pollStatus(); pollEvents(); }, 1500);
</script>
</body>
</html>
"##;
