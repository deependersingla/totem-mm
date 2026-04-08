pub const INDEX_HTML: &str = r##"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TOTEM</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;padding:0;padding-bottom:70px}
h1{font-size:20px;color:#58a6ff}
h2{font-size:14px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:900px;margin:0 auto;padding:16px}
.full{grid-column:1/-1}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
.status-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
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
.btn-signal.boundary{background:#1f6feb;color:#fff}
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

/* Sticky header */
.sticky-header{position:sticky;top:0;z-index:50;background:#0f1117;border-bottom:1px solid #30363d;padding:10px 16px}
.header-inner{max-width:900px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}

/* Bottom tab bar */
.bottom-bar{position:fixed;bottom:0;left:0;right:0;background:#161b22;border-top:1px solid #30363d;display:flex;justify-content:center;gap:0;padding:8px 16px;z-index:100}
.tab-btn{flex:1;max-width:200px;padding:10px 16px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;background:transparent;color:#8b949e;transition:all 0.2s}
.tab-btn.tab-active{background:#238636;color:#fff}
.tab-btn:hover:not(.tab-active){background:#21262d;color:#e1e4e8}

/* Signal buttons */
.signal-btn{padding:14px 32px;border:none;border-radius:10px;font-size:18px;font-weight:700;cursor:pointer;min-width:120px;transition:all 0.15s;text-transform:uppercase;letter-spacing:1px}
.signal-btn:disabled{opacity:0.3;cursor:not-allowed}
.signal-btn.wicket{background:#da3633;color:#fff}
.signal-btn.wicket:hover:not(:disabled){background:#f85149}
.signal-btn.boundary{background:#1f6feb;color:#fff}
.signal-btn.boundary:hover:not(:disabled){background:#388bfd}
.signal-btn.boundary-6{background:#1158c7;color:#fff}
.signal-btn.boundary-6:hover:not(:disabled){background:#1f6feb}

/* Latency bar */
.latency-bar{font-size:12px;color:#8b949e;font-family:'SF Mono',Monaco,Consolas,monospace;padding:8px 12px;background:#0d1117;border-radius:6px;text-align:center}
</style>
</head>
<body>

<!-- Sticky Header -->
<div class="sticky-header">
  <div class="header-inner">
    <h1>TOTEM</h1>
    <div class="status-bar">
      <span id="phaseBadge" class="badge badge-idle">IDLE</span>
      <span id="dryBadge" class="badge badge-dry" style="display:none">DRY RUN</span>
      <span id="headerTeams" style="font-size:13px;color:#c9d1d9;font-weight:600"></span>
      <span id="headerBatting" style="font-size:12px;color:#8b949e"></span>
      <span id="headerInnings" style="font-size:12px;color:#8b949e"></span>
    </div>
  </div>
</div>

<!-- Main Content -->
<div class="grid">

<!-- ======================== SHARED SECTIONS ======================== -->

<!-- Order Book (BBO + L2 depth) -->
<div class="card full shared-section">
  <h2>Order Book <span id="bookUpdated" style="font-size:10px;color:#484f58;font-weight:normal;margin-left:8px"></span></h2>
  <!-- BBO summary -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:10px">
    <div>
      <div style="font-size:12px;color:#8b949e;margin-bottom:4px" id="bookALabel">Team A</div>
      <div class="book-row"><span class="book-bid" id="aBid">--</span><span class="book-ask" id="aAsk">--</span></div>
    </div>
    <div>
      <div style="font-size:12px;color:#8b949e;margin-bottom:4px" id="bookBLabel">Team B</div>
      <div class="book-row"><span class="book-bid" id="bBid">--</span><span class="book-ask" id="bAsk">--</span></div>
    </div>
  </div>
  <!-- L2 depth -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
    <div>
      <div style="font-size:12px;color:#8b949e;margin-bottom:6px;font-weight:600" id="bookLabelA">Team A</div>
      <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace">
        <thead><tr>
          <th style="text-align:left;color:#3fb950;padding:2px 6px;border-bottom:1px solid #21262d">BID</th>
          <th style="text-align:right;color:#3fb950;padding:2px 6px;border-bottom:1px solid #21262d">SIZE</th>
          <th style="width:12px;border-bottom:1px solid #21262d"></th>
          <th style="text-align:left;color:#f85149;padding:2px 6px;border-bottom:1px solid #21262d">ASK</th>
          <th style="text-align:right;color:#f85149;padding:2px 6px;border-bottom:1px solid #21262d">SIZE</th>
        </tr></thead>
        <tbody id="bookBodyA"></tbody>
      </table>
    </div>
    <div>
      <div style="font-size:12px;color:#8b949e;margin-bottom:6px;font-weight:600" id="bookLabelB">Team B</div>
      <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace">
        <thead><tr>
          <th style="text-align:left;color:#3fb950;padding:2px 6px;border-bottom:1px solid #21262d">BID</th>
          <th style="text-align:right;color:#3fb950;padding:2px 6px;border-bottom:1px solid #21262d">SIZE</th>
          <th style="width:12px;border-bottom:1px solid #21262d"></th>
          <th style="text-align:left;color:#f85149;padding:2px 6px;border-bottom:1px solid #21262d">ASK</th>
          <th style="text-align:right;color:#f85149;padding:2px 6px;border-bottom:1px solid #21262d">SIZE</th>
        </tr></thead>
        <tbody id="bookBodyB"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Signals -->
<div class="card full shared-section">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <h2 style="margin-bottom:0">Signals</h2>
    <div style="display:flex;gap:14px;align-items:center">
      <span style="font-size:11px;color:#8b949e;font-weight:600">Trade on:</span>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer;margin:0">
        <input type="checkbox" id="chkTeamA" checked onchange="toggleTeam('A', this.checked)" style="width:15px;height:15px;accent-color:#238636;cursor:pointer">
        <span id="chkTeamALabel" style="font-size:12px;color:#e1e4e8">Team A</span>
      </label>
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer;margin:0">
        <input type="checkbox" id="chkTeamB" checked onchange="toggleTeam('B', this.checked)" style="width:15px;height:15px;accent-color:#238636;cursor:pointer">
        <span id="chkTeamBLabel" style="font-size:12px;color:#e1e4e8">Team B</span>
      </label>
    </div>
  </div>
  <!-- Big 3 signal buttons -->
  <div style="display:flex;gap:12px;justify-content:center;margin-bottom:12px">
    <button class="signal-btn wicket" onclick="sendSignal('W')">WICKET</button>
    <button class="signal-btn boundary" onclick="sendSignal('4')">FOUR</button>
    <button class="signal-btn boundary-6" onclick="sendSignal('6')">SIX</button>
  </div>
  <!-- Toggle for advanced signals -->
  <div style="text-align:center;margin-bottom:8px">
    <button style="background:transparent;color:#58a6ff;border:none;font-size:12px;cursor:pointer;text-decoration:underline" onclick="toggleAdvancedSignals()">
      <span id="advSignalToggleText">Show all signals</span>
    </button>
  </div>
  <!-- Advanced signal grid (hidden by default) -->
  <div id="advancedSignals" style="display:none">
    <div style="font-size:11px;color:#8b949e;margin-bottom:6px">Runs -- <span style="color:#1f6feb">blue=boundary (sell bowling/buy batting + revert)</span></div>
    <div class="signal-grid">
      <button class="btn-signal" onclick="sendSignal('0')">0</button>
      <button class="btn-signal" onclick="sendSignal('1')">1</button>
      <button class="btn-signal" onclick="sendSignal('2')">2</button>
      <button class="btn-signal" onclick="sendSignal('3')">3</button>
      <button class="btn-signal boundary" onclick="sendSignal('4')">4</button>
      <button class="btn-signal" onclick="sendSignal('5')">5</button>
      <button class="btn-signal boundary" onclick="sendSignal('6')">6</button>
    </div>
    <div style="font-size:11px;color:#f85149;margin-top:8px;margin-bottom:6px">Wicket (+ runs on that ball)</div>
    <div class="signal-grid">
      <button class="btn-signal wicket" onclick="sendSignal('W')">W</button>
      <button class="btn-signal wicket" onclick="sendSignal('W1')">W1</button>
      <button class="btn-signal wicket" onclick="sendSignal('W2')">W2</button>
      <button class="btn-signal wicket" onclick="sendSignal('W3')">W3</button>
      <button class="btn-signal wicket" onclick="sendSignal('W4')">W4</button>
      <button class="btn-signal wicket" onclick="sendSignal('W5')">W5</button>
      <button class="btn-signal wicket" onclick="sendSignal('W6')">W6</button>
    </div>
    <div style="font-size:11px;color:#d29922;margin-top:8px;margin-bottom:6px">Wide (+ extra runs) -- Wd4/Wd6 = boundary</div>
    <div class="signal-grid">
      <button class="btn-signal" style="background:#3d2d00;color:#d29922" onclick="sendSignal('Wd0')">Wd0</button>
      <button class="btn-signal" style="background:#3d2d00;color:#d29922" onclick="sendSignal('Wd1')">Wd1</button>
      <button class="btn-signal" style="background:#3d2d00;color:#d29922" onclick="sendSignal('Wd2')">Wd2</button>
      <button class="btn-signal" style="background:#3d2d00;color:#d29922" onclick="sendSignal('Wd3')">Wd3</button>
      <button class="btn-signal boundary" onclick="sendSignal('Wd4')">Wd4</button>
      <button class="btn-signal" style="background:#3d2d00;color:#d29922" onclick="sendSignal('Wd5')">Wd5</button>
      <button class="btn-signal boundary" onclick="sendSignal('Wd6')">Wd6</button>
    </div>
    <div style="font-size:11px;color:#8b949e;margin-top:8px;margin-bottom:6px">No Ball (+ runs) -- N4/N6 = boundary</div>
    <div class="signal-grid">
      <button class="btn-signal" style="background:#1c2333;color:#58a6ff" onclick="sendSignal('N0')">N0</button>
      <button class="btn-signal" style="background:#1c2333;color:#58a6ff" onclick="sendSignal('N1')">N1</button>
      <button class="btn-signal" style="background:#1c2333;color:#58a6ff" onclick="sendSignal('N2')">N2</button>
      <button class="btn-signal" style="background:#1c2333;color:#58a6ff" onclick="sendSignal('N3')">N3</button>
      <button class="btn-signal boundary" onclick="sendSignal('N4')">N4</button>
      <button class="btn-signal" style="background:#1c2333;color:#58a6ff" onclick="sendSignal('N5')">N5</button>
      <button class="btn-signal boundary" onclick="sendSignal('N6')">N6</button>
    </div>
  </div>
</div>

<!-- Latency Bar -->
<div class="card full shared-section">
  <div id="latBar" class="latency-bar">Sig-Dec: -- | Sign-Post: -- | Post-Resp: -- | Fill(WS): -- | E2E: --</div>
</div>


<!-- Event Log -->
<div class="card full shared-section">
  <h2>Event Log</h2>
  <div class="events" id="eventLog"></div>
</div>

<!-- ======================== TAKER TAB ======================== -->
<div id="tab-taker" class="tab-content">

  <!-- Taker Position Card -->
  <div class="card full">
    <h2>Position</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div class="stat"><span id="teamALabel">TEAM_A</span> <strong id="teamATokens">0</strong></div>
      <div class="stat"><span id="teamBLabel">TEAM_B</span> <strong id="teamBTokens">0</strong></div>
      <div class="stat"><span>Spent</span> <strong id="spent">0</strong> / <strong id="budget">100</strong></div>
      <div class="stat"><span>Remaining</span> <strong id="remaining">100</strong></div>
      <div class="stat"><span>Trades</span> <strong id="trades">0</strong></div>
      <div class="stat"><span>Live Orders</span> <strong id="liveOrders">0</strong></div>
      <div class="stat"><span>Batting</span> <strong id="batting">--</strong></div>
      <div class="stat"><span>Bowling</span> <strong id="bowling">--</strong></div>
      <div class="stat"><span>Innings</span> <strong id="innings">1</strong></div>
    </div>
  </div>

  <!-- Taker Limits Card -->
  <div class="card full">
    <h2>Limits</h2>
    <div class="row">
      <div><label>Budget ($)</label><input id="lBudget" type="number" value="100"></div>
      <div><label>Max Trade ($)</label><input id="lMaxTrade" type="number" value="10"></div>
    </div>
    <div class="row">
      <div><label>Safe % (cents)</label><input id="lSafePct" type="number" value="2" min="1" max="49"></div>
      <div><label>Revert Delay (ms)</label><input id="lDelay" type="number" value="3000"></div>
    </div>
    <div class="row">
      <div><label>Fill Poll (ms)</label><input id="lPollInterval" type="number" value="500" min="100"></div>
      <div><label>Poll Timeout (ms)</label><input id="lPollTimeout" type="number" value="5000" min="1000"></div>
    </div>
    <div class="row">
      <div><label>Dry Run</label>
        <select id="lDryRun"><option value="true">Yes</option><option value="false">No</option></select>
      </div>
    </div>
    <div style="border-top:1px solid #21262d;margin-top:10px;padding-top:8px">
      <div style="font-size:12px;color:#8b949e;font-weight:600;margin-bottom:6px">Revert Edge (profit margin on GTC limit orders)</div>
      <div class="row">
        <div><label>Wicket W (%)</label><input id="lEdgeW" type="number" value="2" min="0" max="50" step="0.1"></div>
        <div><label>Boundary 4 (%)</label><input id="lEdge4" type="number" value="1" min="0" max="50" step="0.1"></div>
        <div><label>Boundary 6 (%)</label><input id="lEdge6" type="number" value="1" min="0" max="50" step="0.1"></div>
      </div>
      <div style="font-size:11px;color:#8b949e;margin-top:4px">SELL revert: limit = buy_price x (1 + edge%). BUY revert: limit = sell_price x (1 - edge%). Higher edge = more profit per fill but slower to fill.</div>
    </div>
    <div style="font-size:11px;color:#8b949e;margin-top:4px">Safe %: skip trades when price &lt; X cents or &gt; (100-X) cents. Fill poll: how often to check FAK fill status before placing GTC revert.</div>
    <button class="btn-primary" style="margin-top:10px;width:100%" onclick="saveLimits()">Save Limits</button>
  </div>

  <!-- Match Controls Card -->
  <div class="card full">
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

  <!-- Taker Trade Log -->
  <div class="card full">
    <h2>Trade Log <span style="font-size:10px;color:#484f58;font-weight:normal;margin-left:6px">(from Polymarket CLOB)</span></h2>
    <div id="tradeSummary" style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;font-size:12px"></div>
    <div style="max-height:300px;overflow-y:auto">
      <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace">
        <thead><tr style="border-bottom:2px solid #30363d">
          <th style="text-align:left;padding:4px 6px;color:#8b949e">Time</th>
          <th style="text-align:left;padding:4px 6px;color:#8b949e">Side</th>
          <th style="text-align:left;padding:4px 6px;color:#8b949e">Team</th>
          <th style="text-align:right;padding:4px 6px;color:#8b949e">Filled</th>
          <th style="text-align:right;padding:4px 6px;color:#8b949e">Price</th>
          <th style="text-align:right;padding:4px 6px;color:#8b949e">Cost</th>
          <th style="text-align:left;padding:4px 6px;color:#8b949e">Type</th>
          <th style="text-align:left;padding:4px 6px;color:#8b949e">Status</th>
        </tr></thead>
        <tbody id="tradeBody"></tbody>
      </table>
    </div>
    <div id="tradeEmpty" style="font-size:12px;color:#484f58;padding:8px 0">No trades yet</div>
  </div>

  <!-- Open / Pending Orders + Round-Trip PnL -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px" class="full">
    <div class="card">
      <h2>Open Orders <span style="font-size:10px;color:#484f58;font-weight:normal">(CLOB)</span></h2>
      <div style="max-height:250px;overflow-y:auto">
        <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace">
          <thead><tr style="border-bottom:2px solid #30363d">
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Side</th>
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Team</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Price</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Size</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Filled</th>
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Status</th>
            <th style="padding:4px 6px"></th>
          </tr></thead>
          <tbody id="openOrdersBody"></tbody>
        </table>
      </div>
      <div id="openOrdersEmpty" style="font-size:12px;color:#484f58;padding:8px 0">No open orders</div>

      <h2 style="margin-top:12px">Pending Reverts</h2>
      <div style="max-height:200px;overflow-y:auto">
        <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace">
          <thead><tr style="border-bottom:2px solid #30363d">
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Label</th>
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Side</th>
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Team</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Entry</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Limit</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Age</th>
            <th style="padding:4px 6px"></th>
          </tr></thead>
          <tbody id="revertsBody"></tbody>
        </table>
      </div>
      <div id="revertsEmpty" style="font-size:12px;color:#484f58;padding:8px 0">No pending reverts</div>
    </div>

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h2>Round-Trip PnL</h2>
        <span id="rtPnlTotal" style="font-size:16px;font-weight:700;color:#8b949e">$0.00</span>
      </div>
      <div style="max-height:450px;overflow-y:auto">
        <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace">
          <thead><tr style="border-bottom:2px solid #30363d">
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Signal</th>
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Side</th>
            <th style="text-align:left;padding:4px 6px;color:#8b949e">Team</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Entry</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Exit</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">Size</th>
            <th style="text-align:right;padding:4px 6px;color:#8b949e">PnL</th>
          </tr></thead>
          <tbody id="rtBody"></tbody>
        </table>
      </div>
      <div id="rtEmpty" style="font-size:12px;color:#484f58;padding:8px 0">No round-trips yet</div>
    </div>
  </div>

</div>

<!-- ======================== SETTINGS TAB ======================== -->
<div id="tab-settings" class="tab-content" style="display:none">

  <!-- Match Setup Card -->
  <div class="card full">
    <h2>Match Setup</h2>
    <div id="setupLock">
      <div class="row" style="margin-bottom:6px">
        <div style="flex:3"><label>Polymarket Slug</label><input id="sSlug" value="" placeholder="e.g. crint-ind-zwe-2026-02-26"></div>
        <div style="flex:1;display:flex;align-items:flex-end"><button class="btn-primary" style="width:100%" onclick="fetchMarket()">Fetch</button></div>
      </div>
      <div class="row">
        <div><label>Team A</label><input id="sTeamA" value=""></div>
        <div><label>Team B</label><input id="sTeamB" value=""></div>
      </div>
      <label>Team A Token ID</label><input id="sTokenA" value="" style="font-size:10px">
      <label>Team B Token ID</label><input id="sTokenB" value="" style="font-size:10px">
      <label>Condition ID</label><input id="sCondition" value="" placeholder="0x..." style="font-size:10px">
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

  <!-- Wallet Card -->
  <div class="card full">
    <h2>Wallet</h2>
    <div id="walletLock">
      <label>MetaMask Private Key (signs everything, pays gas)</label>
      <input id="wKey" type="password" placeholder="0x... (your MetaMask private key)">
      <label style="color:#58a6ff">EOA Address (derived)</label>
      <input id="wEoa" readonly placeholder="set private key and save" style="font-size:10px;color:#58a6ff;cursor:default">
      <label>Polymarket Proxy Wallet Address (holds USDC, maker of CLOB trades)</label>
      <input id="wAddr" placeholder="0x... (your Polymarket proxy -- NOT MetaMask address)">
      <div class="row">
        <div><label>Sig Type</label>
          <select id="wSigType">
            <option value="1">POLY_PROXY (1) -- MetaMask + Polymarket proxy</option>
            <option value="0">EOA (0) -- no proxy, direct wallet</option>
            <option value="2">GNOSIS_SAFE (2)</option>
          </select>
        </div>
      </div>
      <div style="font-size:11px;color:#8b949e;margin-top:6px;line-height:1.5">
        <b>Most users:</b> Private Key = MetaMask key . Address = Polymarket proxy (shown at polymarket.com when logged in) . Sig Type = POLY_PROXY (1)
      </div>
      <button class="btn-primary" style="margin-top:10px;width:100%" onclick="saveWallet()">Save Wallet</button>
      <div id="wDeriveStatus" style="margin-top:8px;font-size:12px;display:none">
        <div style="color:#3fb950;font-weight:600">API Key derived via EIP-712 signing</div>
        <div style="color:#8b949e;font-size:11px;margin-top:2px">Key: <span id="wDerivedKey" style="color:#58a6ff;font-family:monospace"></span></div>
      </div>
    </div>
    <div id="walletLockedMsg" class="locked-notice" style="display:none">Wallet locked while match is running</div>
    <div class="stat" style="margin-top:6px"><span>Status</span> <strong id="walletStatus">Not Set</strong></div>
  </div>

  <!-- Wallets & Balances Card -->
  <div class="card full">
    <h2>Wallets &amp; Balances <button class="btn-primary" style="font-size:11px;padding:3px 10px;float:right" onclick="refreshWallets()">Refresh</button></h2>
    <div style="margin-bottom:8px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:4px">EOA (MetaMask -- signs, pays gas)</div>
      <div style="font-size:10px;color:#58a6ff;word-break:break-all;font-family:monospace" id="wBalEoa">--</div>
      <div class="stat"><span>USDC (EOA)</span> <strong id="wBalEoaUsdc">--</strong></div>
    </div>
    <div>
      <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Proxy (Polymarket -- holds USDC, maker of trades)</div>
      <div style="font-size:10px;color:#3fb950;word-break:break-all;font-family:monospace" id="wBalProxy">--</div>
      <div class="stat"><span>USDC (Proxy)</span> <strong id="wBalProxyUsdc">--</strong></div>
    </div>
    <div id="wPositions" style="margin-top:8px;font-size:11px"></div>
  </div>

  <!-- CTF On-Chain Card -->
  <div class="card full">
    <h2>CTF On-Chain</h2>
    <div class="row" style="margin-bottom:8px">
      <div style="flex:2">
        <label>CTF Amount (USDC / tokens)</label>
        <input id="ctfAmount" type="number" value="10" min="1" step="1">
      </div>
      <div style="flex:3;display:flex;gap:8px;align-items:flex-end">
        <button class="btn-primary" style="flex:1" onclick="ctfSplit()">Split USDC Tokens</button>
        <button class="btn-warn" style="flex:1" onclick="ctfMerge()">Merge Tokens USDC</button>
        <button class="btn-danger" style="flex:1" onclick="ctfRedeem()">Redeem (Post-Resolve)</button>
      </div>
    </div>
    <div style="border-top:1px solid #21262d;padding-top:8px;margin-top:4px">
      <div style="font-size:12px;color:#8b949e;margin-bottom:6px;font-weight:600">Move Funds (EOA / Proxy)</div>
      <div class="row" style="margin-bottom:6px">
        <button class="btn-primary" style="flex:1" onclick="moveTokens('to_proxy')">Move All Tokens to Proxy</button>
        <button class="btn-warn" style="flex:1" onclick="moveTokens('to_eoa')">Move All Tokens to EOA</button>
      </div>
      <div class="row" style="margin-top:6px">
        <div style="flex:2">
          <label>USDC Amount ($)</label>
          <input id="moveUsdcAmt" type="number" value="50" min="1" step="1">
        </div>
        <div style="flex:3;display:flex;gap:8px;align-items:flex-end">
          <button class="btn-primary" style="flex:1" onclick="moveUsdc('to_proxy')">USDC to Proxy</button>
          <button class="btn-warn" style="flex:1" onclick="moveUsdc('to_eoa')">USDC to EOA</button>
        </div>
      </div>
    </div>
    <div style="margin-top:8px;display:flex;gap:8px">
      <button class="btn-primary" style="background:#30363d" onclick="ctfSyncBalance()">Sync On-Chain Balances</button>
      <button class="btn-primary" style="background:#6e40c9" onclick="megaResolve()">Mega Resolve (All Positions)</button>
    </div>
    <div style="font-size:11px;color:#8b949e;margin-top:6px;line-height:1.6">
      <b>Split:</b> $X USDC into X YES + X NO |
      <b>Merge:</b> X YES + X NO into $X USDC |
      <b>Redeem:</b> winning tokens into USDC (post-resolve) |
      <b>Tokens to Proxy:</b> move YES+NO from your MetaMask wallet into the proxy for CLOB trading |
      <b>USDC to Proxy:</b> deposit USDC into proxy before splitting
    </div>
  </div>

</div>

</div><!-- end .grid -->

<!-- Bottom Tab Bar -->
<div class="bottom-bar">
  <button class="tab-btn tab-active" data-tab="taker" onclick="switchTab('taker')">&#9889; TAKER</button>
  <button class="tab-btn" data-tab="settings" onclick="switchTab('settings')">&#9881; SETTINGS</button>
</div>

<script>
const API = '';
let pollTimer = null;
let activeTab = 'taker';
let advancedSignalsVisible = false;

async function api(path, opts) {
  try {
    const r = await fetch(API + path, opts);
    const text = await r.text();
    let j;
    try { j = text ? JSON.parse(text) : {}; } catch(_) { j = {raw: text}; }
    if (!r.ok) throw new Error(j.detail || j.error || j.raw || r.statusText);
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

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
  document.getElementById('tab-' + tab).style.display = '';

  // Show/hide shared sections (visible on taker+maker, hidden on settings)
  const showShared = tab !== 'settings';
  document.querySelectorAll('.shared-section').forEach(el => el.style.display = showShared ? '' : 'none');

  // Update tab bar active state
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('tab-active'));
  document.querySelector('[data-tab="' + tab + '"]').classList.add('tab-active');
}

function toggleAdvancedSignals() {
  advancedSignalsVisible = !advancedSignalsVisible;
  document.getElementById('advancedSignals').style.display = advancedSignalsVisible ? '' : 'none';
  document.getElementById('advSignalToggleText').textContent = advancedSignalsVisible ? 'Hide all signals' : 'Show all signals';
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

    // header info
    el('headerTeams').textContent = s.team_a_name && s.team_b_name ? s.team_a_name + ' vs ' + s.team_b_name : '';
    el('headerBatting').textContent = s.batting ? 'Bat: ' + s.batting : '';
    el('headerInnings').textContent = s.innings ? 'Inn ' + s.innings : '';

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
    el('aBid').textContent = s.book_a_bid != null ? s.book_a_bid+'c' : '--';
    el('aAsk').textContent = s.book_a_ask != null ? s.book_a_ask+'c' : '--';
    el('bBid').textContent = s.book_b_bid != null ? s.book_b_bid+'c' : '--';
    el('bAsk').textContent = s.book_b_ask != null ? s.book_b_ask+'c' : '--';

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
    document.querySelectorAll('.signal-btn').forEach(b => b.disabled = !running);

    // sync trade-on checkboxes
    el('chkTeamA').checked = s.trade_team_a;
    el('chkTeamB').checked = s.trade_team_b;
    el('chkTeamALabel').textContent = s.team_a_name;
    el('chkTeamBLabel').textContent = s.team_b_name;

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
      return '<div class="' + cls + '"><span class="ev-ts">' + e.ts + '</span><span class="ev-kind">' + e.kind + '</span><span class="ev-detail">' + e.detail + '</span></div>';
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
    if (c.market_slug) document.getElementById('sSlug').value = c.market_slug;
    document.getElementById('sBatFirst').value = c.first_batting === 'TEAM_B' ? 'B' : 'A';
    document.getElementById('sNegRisk').value = String(c.neg_risk);
    document.getElementById('lBudget').value = c.total_budget_usdc;
    document.getElementById('lMaxTrade').value = c.max_trade_usdc;
    document.getElementById('lSafePct').value = c.safe_percentage;
    document.getElementById('lDelay').value = c.revert_delay_ms;
    document.getElementById('lPollInterval').value = c.fill_poll_interval_ms;
    document.getElementById('lPollTimeout').value = c.fill_poll_timeout_ms;
    document.getElementById('lDryRun').value = String(c.dry_run);
    if (c.edge_wicket != null) document.getElementById('lEdgeW').value = c.edge_wicket;
    if (c.edge_boundary_4 != null) document.getElementById('lEdge4').value = c.edge_boundary_4;
    if (c.edge_boundary_6 != null) document.getElementById('lEdge6').value = c.edge_boundary_6;
    document.getElementById('wSigType').value = c.signature_type;
    if (c.polymarket_address) document.getElementById('wAddr').value = c.polymarket_address;
    if (c.eoa_address) document.getElementById('wEoa').value = c.eoa_address;
    const wk = document.getElementById('wKey');
    if (c.private_key_set) {
      wk.placeholder = '********** (already set)';
    }
    const ds = document.getElementById('wDeriveStatus');
    if (c.api_key_set && c.api_key_id) {
      ds.style.display = '';
      document.getElementById('wDerivedKey').textContent = c.api_key_id;
    } else {
      ds.style.display = c.api_key_set ? '' : 'none';
      if (c.api_key_set) document.getElementById('wDerivedKey').textContent = '(configured)';
    }
  } catch(e) {}
}

async function refreshWallets() {
  try {
    const w = await api('/api/wallets');
    document.getElementById('wBalEoa').textContent = w.eoa_address || '--';
    document.getElementById('wBalProxy').textContent = w.proxy_address || '--';
    document.getElementById('wBalEoaUsdc').textContent = w.eoa_usdc != null ? '$' + parseFloat(w.eoa_usdc).toFixed(2) : '--';
    document.getElementById('wBalProxyUsdc').textContent = w.proxy_usdc != null ? '$' + parseFloat(w.proxy_usdc).toFixed(2) : '--';
    // Render positions
    const pos = document.getElementById('wPositions');
    const positions = Array.isArray(w.positions) ? w.positions : [];
    if (positions.length === 0) {
      pos.textContent = '';
    } else {
      pos.innerHTML = '<div style="color:#8b949e;margin-bottom:4px">Open Positions (proxy)</div>' +
        positions.slice(0, 10).map(p => {
          const slug = p.market?.slug || p.conditionId || '?';
          const outcome = p.outcome || p.side || '?';
          const size = parseFloat(p.size || 0).toFixed(2);
          const val = p.currentValue != null ? ' ($' + parseFloat(p.currentValue).toFixed(2) + ')' : '';
          return '<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #21262d"><span style="color:#c9d1d9;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%">' + slug + '</span><span style="color:#3fb950;white-space:nowrap">' + outcome + ' ' + size + val + '</span></div>';
        }).join('');
    }
    if (w.eoa_address) document.getElementById('wEoa').value = w.eoa_address;
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
  const r = await api('/api/wallet', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if (r && r.api_key) {
    const ds = document.getElementById('wDeriveStatus');
    ds.style.display = '';
    document.getElementById('wDerivedKey').textContent = r.api_key;
  }
  await loadConfig();
  refreshWallets();
}

async function saveLimits() {
  await api('/api/limits', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
    total_budget_usdc: document.getElementById('lBudget').value,
    max_trade_usdc: document.getElementById('lMaxTrade').value,
    safe_percentage: parseInt(document.getElementById('lSafePct').value),
    revert_delay_ms: parseInt(document.getElementById('lDelay').value),
    fill_poll_interval_ms: parseInt(document.getElementById('lPollInterval').value),
    fill_poll_timeout_ms: parseInt(document.getElementById('lPollTimeout').value),
    dry_run: document.getElementById('lDryRun').value === 'true',
    edge_wicket: parseFloat(document.getElementById('lEdgeW').value),
    edge_boundary_4: parseFloat(document.getElementById('lEdge4').value),
    edge_boundary_6: parseFloat(document.getElementById('lEdge6').value),
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
async function ctfSyncBalance() {
  await api('/api/ctf-balance', {method:'POST'});
}
async function moveTokens(direction) {
  const label = direction === 'to_proxy' ? 'EOA to Proxy' : 'Proxy to EOA';
  if (!confirm('Move ALL YES + NO tokens (' + label + ')?\nThis transfers your full token balance.')) return;
  await api('/api/move-tokens', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({direction: direction})});
}
async function moveUsdc(direction) {
  const amt = parseInt(document.getElementById('moveUsdcAmt').value);
  if (!amt || amt <= 0) { showToast('enter a positive amount'); return; }
  const label = direction === 'to_proxy' ? 'EOA to Proxy' : 'Proxy to EOA';
  if (!confirm('Move $' + amt + ' USDC (' + label + ')?')) return;
  await api('/api/move-usdc', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({amount_usdc: amt, direction: direction})});
}
async function toggleTeam(team, enabled) {
  await api('/api/toggle-team', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({team: team, enabled: enabled})});
}

async function megaResolve() {
  if (!confirm('Try to redeem ALL resolved positions? This will attempt redeem on every condition_id found in your open positions.')) return;
  await api('/api/mega-resolve', {method:'POST'});
}

async function fetchMarket() {
  const slug = document.getElementById('sSlug').value.trim();
  if (!slug) { showToast('enter a market slug'); return; }
  const r = await api('/api/fetch-market', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({slug: slug})});
  if (r && r.ok) {
    document.getElementById('sTeamA').value = r.team_a_name;
    document.getElementById('sTeamB').value = r.team_b_name;
    document.getElementById('sTokenA').value = r.team_a_token_id;
    document.getElementById('sTokenB').value = r.team_b_token_id;
    document.getElementById('sCondition').value = r.condition_id;
    document.getElementById('sNegRisk').value = String(r.neg_risk);
    pollPriceChart();
  }
}

async function pollTrades() {
  try {
    const d = await api('/api/trades');
    const body = document.getElementById('tradeBody');
    const empty = document.getElementById('tradeEmpty');
    if (d.error) return;
    const trades = d.trades || [];
    if (trades.length === 0) {
      body.innerHTML = '';
      empty.style.display = '';
      document.getElementById('tradeSummary').innerHTML = '';
      return;
    }
    empty.style.display = 'none';
    body.innerHTML = trades.slice().reverse().map(t => {
      const sideColor = t.side === 'BUY' ? '#3fb950' : '#f85149';
      const cost = parseFloat(t.cost).toFixed(2);
      const statusColor = t.status === 'MATCHED' ? '#3fb950' : t.status === 'LIVE' ? '#58a6ff' : '#8b949e';
      return '<tr style="border-bottom:1px solid #21262d">' +
        '<td style="padding:3px 6px;color:#484f58">' + t.ts + '</td>' +
        '<td style="padding:3px 6px;color:' + sideColor + ';font-weight:600">' + t.side + '</td>' +
        '<td style="padding:3px 6px;color:#e1e4e8">' + t.team + '</td>' +
        '<td style="padding:3px 6px;text-align:right;color:#e1e4e8">' + t.size + '</td>' +
        '<td style="padding:3px 6px;text-align:right;color:#e1e4e8">' + t.price + '</td>' +
        '<td style="padding:3px 6px;text-align:right;color:#e1e4e8">$' + cost + '</td>' +
        '<td style="padding:3px 6px;color:#8b949e">' + t.order_type + '</td>' +
        '<td style="padding:3px 6px;color:' + statusColor + '">' + t.status + '</td>' +
        '</tr>';
    }).join('');
    // Render summary
    const s = d.summary;
    const a = s.team_a, b = s.team_b;
    const pnlColor = v => parseFloat(v) >= 0 ? '#3fb950' : '#f85149';
    const fmt = v => parseFloat(v).toFixed(2);
    const fmtPnl = v => (parseFloat(v) >= 0 ? '+$' : '-$') + Math.abs(parseFloat(v)).toFixed(2);
    document.getElementById('tradeSummary').innerHTML =
      '<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 12px;flex:1;min-width:200px">' +
        '<div style="font-weight:600;color:#e1e4e8;margin-bottom:4px">' + a.name + '</div>' +
        '<div>Bought: <strong>' + a.bought + '</strong> @ avg <strong>' + fmt(a.avg_buy) + '</strong> = $' + fmt(a.buy_cost) + '</div>' +
        '<div>Sold: <strong>' + a.sold + '</strong> @ avg <strong>' + fmt(a.avg_sell) + '</strong> = $' + fmt(a.sell_revenue) + '</div>' +
        '<div>Net tokens: <strong>' + a.net_tokens + '</strong></div>' +
        '<div>P&L: <strong style="color:' + pnlColor(a.realized_pnl) + '">' + fmtPnl(a.realized_pnl) + '</strong></div>' +
      '</div>' +
      '<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 12px;flex:1;min-width:200px">' +
        '<div style="font-weight:600;color:#e1e4e8;margin-bottom:4px">' + b.name + '</div>' +
        '<div>Bought: <strong>' + b.bought + '</strong> @ avg <strong>' + fmt(b.avg_buy) + '</strong> = $' + fmt(b.buy_cost) + '</div>' +
        '<div>Sold: <strong>' + b.sold + '</strong> @ avg <strong>' + fmt(b.avg_sell) + '</strong> = $' + fmt(b.sell_revenue) + '</div>' +
        '<div>Net tokens: <strong>' + b.net_tokens + '</strong></div>' +
        '<div>P&L: <strong style="color:' + pnlColor(b.realized_pnl) + '">' + fmtPnl(b.realized_pnl) + '</strong></div>' +
      '</div>' +
      '<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 12px;display:flex;align-items:center">' +
        '<div>Total P&L: <strong style="color:' + pnlColor(s.total_pnl) + ';font-size:16px">' + fmtPnl(s.total_pnl) + '</strong></div>' +
      '</div>';
  } catch(e) {}
}

async function pollPriceChart() {
  try {
    const interval = document.getElementById('chartInterval').value;
    const d = await api('/api/price-history?interval=' + interval);
    const a = d.team_a || [], b = d.team_b || [];
    if (a.length === 0 && b.length === 0) {
      document.getElementById('chartCard').style.display = 'none';
      return;
    }
    document.getElementById('chartCard').style.display = '';
    drawChart(a, b, d.team_a_name, d.team_b_name);
  } catch(e) {}
}

function drawChart(dataA, dataB, nameA, nameB) {
  const canvas = document.getElementById('priceChart');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const PAD_L = 45, PAD_R = 10, PAD_T = 10, PAD_B = 24;
  const cW = W - PAD_L - PAD_R, cH = H - PAD_T - PAD_B;

  ctx.clearRect(0, 0, W, H);

  // Determine time range from both series
  let allT = [];
  dataA.forEach(p => allT.push(p.t));
  dataB.forEach(p => allT.push(p.t));
  if (allT.length === 0) return;
  const minT = Math.min(...allT), maxT = Math.max(...allT);
  const tRange = maxT - minT || 1;

  // Y axis: 0% to 100%
  const minP = 0, maxP = 1;

  const xOf = t => PAD_L + ((t - minT) / tRange) * cW;
  const yOf = p => PAD_T + (1 - (p - minP) / (maxP - minP)) * cH;

  // Grid lines
  ctx.strokeStyle = '#21262d';
  ctx.lineWidth = 1;
  for (let pct = 0; pct <= 100; pct += 25) {
    const y = yOf(pct / 100);
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
    ctx.fillStyle = '#484f58'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(pct + '%', PAD_L - 4, y + 3);
  }

  // Time labels
  ctx.fillStyle = '#484f58'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
  const nLabels = Math.min(6, allT.length);
  for (let i = 0; i < nLabels; i++) {
    const t = minT + (tRange * i / (nLabels - 1 || 1));
    const d = new Date(t * 1000);
    const lbl = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    ctx.fillText(lbl, xOf(t), H - 4);
  }

  // Draw line
  function drawLine(data, color) {
    if (data.length < 2) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    data.forEach((pt, i) => {
      const x = xOf(pt.t), y = yOf(pt.p);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Glow fill
    ctx.save();
    ctx.globalAlpha = 0.08;
    ctx.fillStyle = color;
    ctx.beginPath();
    data.forEach((pt, i) => {
      const x = xOf(pt.t), y = yOf(pt.p);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.lineTo(xOf(data[data.length-1].t), yOf(0));
    ctx.lineTo(xOf(data[0].t), yOf(0));
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  const colorA = '#3fb950', colorB = '#f85149';
  drawLine(dataA, colorA);
  drawLine(dataB, colorB);

  // Current price labels at the end of lines
  function endLabel(data, color, name) {
    if (data.length === 0) return;
    const last = data[data.length - 1];
    const x = xOf(last.t), y = yOf(last.p);
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
    ctx.font = 'bold 12px sans-serif'; ctx.textAlign = 'left';
    const pct = (last.p * 100).toFixed(0) + '%';
    ctx.fillText(pct, x + 8, y + 4);
  }
  endLabel(dataA, colorA, nameA);
  endLabel(dataB, colorB, nameB);

  // Legend
  const lastA = dataA.length ? (dataA[dataA.length-1].p * 100).toFixed(0) + '%' : '--';
  const lastB = dataB.length ? (dataB[dataB.length-1].p * 100).toFixed(0) + '%' : '--';
  document.getElementById('chartLegend').innerHTML =
    '<span style="color:' + colorA + '">&#9679; ' + nameA + ' ' + lastA + '</span>' +
    '<span style="color:' + colorB + '">&#9679; ' + nameB + ' ' + lastB + '</span>';
}

function renderBookTable(bodyId, bids, asks) {
  const rows = Math.max(bids.length, asks.length, 5);
  let html = '';
  for (let i = 0; i < rows; i++) {
    const bid = bids[i], ask = asks[i];
    html += '<tr>' +
      '<td style="text-align:left;color:#3fb950;padding:3px 6px">' + (bid ? parseFloat(bid.price).toFixed(2) : '--') + '</td>' +
      '<td style="text-align:right;color:#3fb950;padding:3px 6px">' + (bid ? parseFloat(bid.size).toFixed(1) : '') + '</td>' +
      '<td style="border-left:1px solid #21262d"></td>' +
      '<td style="text-align:left;color:#f85149;padding:3px 6px">' + (ask ? parseFloat(ask.price).toFixed(2) : '--') + '</td>' +
      '<td style="text-align:right;color:#f85149;padding:3px 6px">' + (ask ? parseFloat(ask.size).toFixed(1) : '') + '</td>' +
      '</tr>';
  }
  document.getElementById(bodyId).innerHTML = html;
}

async function pollBook() {
  try {
    const b = await api('/api/book');
    document.getElementById('bookLabelA').textContent = b.team_a_name;
    document.getElementById('bookLabelB').textContent = b.team_b_name;
    renderBookTable('bookBodyA', b.team_a_bids, b.team_a_asks);
    renderBookTable('bookBodyB', b.team_b_bids, b.team_b_asks);
    document.getElementById('bookUpdated').textContent = new Date().toLocaleTimeString();
  } catch(e) {}
}

// Latency polling
async function pollLatency() {
  try {
    const l = await api('/api/latency');
    const fmt = (p) => p && p.count > 0 ? (p.p50_us / 1000).toFixed(1) + 'ms' : '--';
    document.getElementById('latBar').innerHTML =
      'Sig&#8594;Dec: ' + fmt(l.signal_to_decision) + ' | ' +
      'Sign&#8594;Post: ' + fmt(l.sign_to_post) + ' | ' +
      'Post&#8594;Resp: ' + fmt(l.post_to_response) + ' | ' +
      'Fill(WS): ' + fmt(l.fill_detect_ws) + ' | ' +
      'E2E: ' + fmt(l.e2e_signal_to_fill);
  } catch(e) {}
}

// ── Open Orders / Pending Reverts / Round-Trip PnL polling ────────────────
async function pollTakerOrders() {
  try {
    const r = await fetch('/api/taker/status');
    if (!r.ok) return;
    const text = await r.text();
    if (!text) return;
    const d = JSON.parse(text);

    // Open orders
    const ooBody = document.getElementById('openOrdersBody');
    const ooEmpty = document.getElementById('openOrdersEmpty');
    if (d.open_orders && d.open_orders.length > 0) {
      ooEmpty.style.display = 'none';
      ooBody.innerHTML = d.open_orders.map(o => {
        const sc = o.side === 'BUY' ? 'color:#3fb950' : 'color:#f85149';
        return `<tr><td style="${sc}">${o.side}</td><td>${o.team}</td><td style="text-align:right">${o.price}</td><td style="text-align:right">${o.original_size}</td><td style="text-align:right">${o.size_matched}</td><td>${o.status}</td><td><button class="btn-danger" style="padding:2px 8px;font-size:10px" onclick="cancelOrder('${o.order_id}')">x</button></td></tr>`;
      }).join('');
    } else {
      ooBody.innerHTML = '';
      ooEmpty.style.display = '';
    }

    // Pending reverts
    const rvBody = document.getElementById('revertsBody');
    const rvEmpty = document.getElementById('revertsEmpty');
    if (d.pending_reverts && d.pending_reverts.length > 0) {
      rvEmpty.style.display = 'none';
      rvBody.innerHTML = d.pending_reverts.map(r => {
        const sc = r.side === 'BUY' ? 'color:#3fb950' : 'color:#f85149';
        return `<tr><td>${r.label}</td><td style="${sc}">${r.side}</td><td>${r.team}</td><td style="text-align:right">${r.entry_price}</td><td style="text-align:right">${r.revert_limit}</td><td style="text-align:right">${r.age_secs.toFixed(0)}s</td><td><button class="btn-danger" style="padding:2px 8px;font-size:10px" onclick="cancelOrder('${r.order_id}')">x</button></td></tr>`;
      }).join('');
    } else {
      rvBody.innerHTML = '';
      rvEmpty.style.display = '';
    }

    // Round-trip PnL
    const rtPnl = parseFloat(d.round_trip_pnl) || 0;
    const rtEl = document.getElementById('rtPnlTotal');
    rtEl.textContent = '$' + rtPnl.toFixed(4);
    rtEl.style.color = rtPnl > 0 ? '#3fb950' : rtPnl < 0 ? '#f85149' : '#8b949e';

    const rtBody = document.getElementById('rtBody');
    const rtEmpty = document.getElementById('rtEmpty');
    if (d.round_trips && d.round_trips.length > 0) {
      rtEmpty.style.display = 'none';
      rtBody.innerHTML = d.round_trips.map(r => {
        const p = parseFloat(r.pnl) || 0;
        const pc = p > 0 ? '#3fb950' : p < 0 ? '#f85149' : '#8b949e';
        return `<tr><td>${r.label}</td><td>${r.entry_side}</td><td>${r.team}</td><td style="text-align:right">${r.entry_price}</td><td style="text-align:right">${r.exit_price}</td><td style="text-align:right">${r.size}</td><td style="text-align:right;color:${pc}">$${p.toFixed(4)}</td></tr>`;
      }).join('');
    } else {
      rtBody.innerHTML = '';
      rtEmpty.style.display = '';
    }
  } catch(e) { console.error('pollTakerOrders', e); }
}

async function cancelOrder(oid) {
  if (!confirm('Cancel order ' + oid.slice(0,12) + '?')) return;
  try {
    await fetch('/api/taker/cancel-order', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({order_id: oid})});
    setTimeout(pollTakerOrders, 500);
  } catch(e) { console.error(e); }
}

// Init
loadConfig();
pollStatus();
pollEvents();
pollTrades();
pollBook();
pollLatency();
pollTakerOrders();
setInterval(pollStatus, 1500);
setInterval(pollEvents, 1500);
setInterval(pollTrades, 2000);
setInterval(pollBook, 500);
setInterval(pollLatency, 3000);
setInterval(pollTakerOrders, 3000);
</script>
</body>
</html>
"##;

pub const SWEEP_HTML: &str = r##"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TOTEM — Sweep</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;padding:12px}
h1{font-size:20px;color:#58a6ff;margin-bottom:4px}
h2{font-size:13px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:960px;margin:0 auto}
.full{grid-column:1/-1}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
.row{display:flex;gap:8px;align-items:center}
.row>*{flex:1}
input,select{background:#0d1117;border:1px solid #30363d;color:#e1e4e8;padding:6px 10px;border-radius:4px;font-size:13px;width:100%}
input:focus{outline:none;border-color:#58a6ff}
label{font-size:12px;color:#8b949e;display:block;margin-bottom:3px;margin-top:8px}
button{padding:8px 14px;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.85}
button:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:#238636;color:#fff}
.btn-warn{background:#d29922;color:#000}
.btn-danger{background:#da3633;color:#fff}
.btn-sm{padding:5px 10px;font-size:12px}
.badge{padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600;text-transform:uppercase;display:inline-block}
.badge-idle{background:#30363d;color:#8b949e}
.badge-active{background:#238636;color:#fff}
.badge-dry{background:#6e40c9;color:#fff;margin-left:6px}
.stat{margin:4px 0}
.stat span{color:#8b949e;font-size:12px}
.stat strong{color:#e1e4e8;font-size:14px;margin-left:4px}
.stat.good strong{color:#3fb950}
.stat.warn strong{color:#d29922}
table{width:100%;border-collapse:collapse;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace}
th{color:#8b949e;font-weight:600;text-align:left;padding:4px 6px;border-bottom:1px solid #30363d;font-size:11px}
td{padding:4px 6px;border-bottom:1px solid #21262d}
.ask{color:#da3633}
.bid{color:#3fb950}
.events{max-height:240px;overflow-y:auto;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace}
.ev{padding:3px 0;border-bottom:1px solid #21262d;display:flex;gap:8px}
.ev-ts{color:#484f58;min-width:55px}
.ev-kind{color:#58a6ff;min-width:50px;font-weight:600}
.ev-detail{color:#c9d1d9;word-break:break-all}
.tab-bar{display:flex;gap:4px;margin-bottom:12px}
.tab{padding:6px 16px;border-radius:6px 6px 0 0;background:#21262d;color:#8b949e;cursor:pointer;font-size:13px;font-weight:600;border:none}
.tab.active{background:#161b22;color:#58a6ff;border:1px solid #30363d;border-bottom-color:#161b22}
.wallet-box{display:flex;gap:12px;align-items:center;padding:8px;background:#0d1117;border-radius:6px;margin:4px 0}
.wallet-box .addr{font-family:monospace;font-size:11px;color:#8b949e;flex:1;overflow:hidden;text-overflow:ellipsis}
.wallet-box .bal{font-size:14px;font-weight:600;color:#e1e4e8}
.wallet-box .label{font-size:11px;color:#58a6ff;font-weight:600;min-width:50px}
.nav{text-align:right;margin-bottom:8px}
.nav a{color:#58a6ff;font-size:12px;text-decoration:none}
</style>
</head>
<body>
<div class="nav"><a href="/">← Main Dashboard</a></div>
<div class="grid">

<!-- Header -->
<div class="card full" style="display:flex;justify-content:space-between;align-items:center">
  <div>
    <h1>TOTEM SWEEP</h1>
    <span style="color:#8b949e;font-size:12px">Endgame position resolution</span>
  </div>
  <div>
    <span id="sweep-badge" class="badge badge-idle">IDLE</span>
    <span id="dry-badge" class="badge badge-dry" style="display:none">DRY RUN</span>
  </div>
</div>

<!-- Wallet Setup -->
<div class="card">
  <h2>Wallet</h2>
  <div id="wallet-locked" style="display:none">
    <div class="wallet-box">
      <span class="label">EOA</span>
      <span class="addr" id="wallet-eoa-display">—</span>
      <span style="font-size:11px;color:#3fb950;font-weight:600">OK</span>
    </div>
    <div class="wallet-box">
      <span class="label">Proxy</span>
      <span class="addr" id="wallet-proxy-display">—</span>
      <span style="font-size:11px;color:#8b949e" id="wallet-sig-display">sig=1</span>
    </div>
    <button class="btn-sm btn-warn" style="margin-top:6px" onclick="unlockWallet()">Edit Wallet</button>
  </div>
  <div id="wallet-form">
    <label>Private Key (EOA) — proxy address is auto-derived</label>
    <input type="password" id="pk" placeholder="0x...">
    <div class="row" style="margin-top:8px">
      <select id="sig-type">
        <option value="0">EOA only (0)</option>
        <option value="1">POLY_PROXY (1)</option>
        <option value="2" selected>GNOSIS_SAFE (2)</option>
      </select>
      <button class="btn-primary btn-sm" onclick="saveWallet()">Save &amp; Derive</button>
    </div>
    <div id="wallet-status" style="font-size:11px;color:#8b949e;margin-top:6px"></div>
  </div>
</div>

<!-- Market Setup -->
<div class="card">
  <h2>Market</h2>
  <label>Polymarket Slug</label>
  <div class="row">
    <input type="text" id="slug" placeholder="e.g. crint-ind-wst-2026-03-29">
    <button class="btn-primary btn-sm" onclick="fetchMarket()">Fetch</button>
  </div>
  <div id="market-info" style="font-size:12px;color:#8b949e;margin-top:8px"></div>
</div>

<!-- Live Balances -->
<div class="card full">
  <h2>Balances <span style="font-size:10px;color:#484f58">(auto-refresh 10s)</span></h2>
  <div class="wallet-box">
    <span class="label">EOA</span>
    <span class="addr" id="eoa-addr">—</span>
    <span class="bal" id="eoa-usdc">—</span>
    <span style="font-size:11px;color:#8b949e">USDC</span>
  </div>
  <div class="wallet-box">
    <span class="label">Proxy</span>
    <span class="addr" id="proxy-addr-display">—</span>
    <span class="bal" id="proxy-usdc">—</span>
    <span style="font-size:11px;color:#8b949e">USDC</span>
  </div>
  <div style="margin-top:8px;font-size:11px;color:#58a6ff;font-weight:600">Tokens in EOA <span style="color:#484f58;font-weight:400">(after split, before move)</span></div>
  <div style="display:flex;gap:12px">
    <div class="wallet-box" style="flex:1">
      <span class="label" id="token-a-label">Team A</span>
      <span class="bal" id="eoa-token-a">—</span>
    </div>
    <div class="wallet-box" style="flex:1">
      <span class="label" id="token-b-label">Team B</span>
      <span class="bal" id="eoa-token-b">—</span>
    </div>
  </div>
  <div style="margin-top:4px;font-size:11px;color:#58a6ff;font-weight:600">Tokens in Proxy <span style="color:#484f58;font-weight:400">(available for CLOB trading)</span></div>
  <div style="display:flex;gap:12px">
    <div class="wallet-box" style="flex:1">
      <span class="label" id="token-a-label-p">Team A</span>
      <span class="bal" id="proxy-token-a">—</span>
    </div>
    <div class="wallet-box" style="flex:1">
      <span class="label" id="token-b-label-p">Team B</span>
      <span class="bal" id="proxy-token-b">—</span>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
    <span id="tick-display" style="font-size:11px;color:#484f58"></span>
    <button class="btn-sm btn-primary" style="padding:2px 8px;font-size:10px" onclick="refreshTick()">Refresh Tick</button>
  </div>
  <div class="row" style="margin-top:8px">
    <button class="btn-sm btn-primary" onclick="moveTokens('to_proxy')">Tokens → Proxy</button>
    <button class="btn-sm btn-primary" onclick="moveTokens('to_eoa')">Tokens → EOA</button>
    <button class="btn-sm btn-primary" onclick="moveUsdc('to_proxy')">USDC → Proxy</button>
    <button class="btn-sm btn-primary" onclick="moveUsdc('to_eoa')">USDC → EOA</button>
  </div>
</div>

<!-- Split Position -->
<div class="card">
  <h2>Split Position</h2>
  <label>Amount USDC (= YES + NO tokens)</label>
  <div class="row">
    <input type="number" id="split-amount" placeholder="1000" value="100">
    <button class="btn-primary btn-sm" onclick="doSplit()">Split</button>
  </div>
  <div id="split-status" style="font-size:11px;color:#8b949e;margin-top:6px"></div>
</div>

<!-- Builder Keys -->
<div class="card">
  <h2>Builder Keys <span style="font-size:10px;color:#484f58">(sweep only)</span></h2>
  <div id="builder-locked" style="display:none">
    <div class="wallet-box">
      <span class="label">Key</span>
      <span class="addr" id="builder-key-display">—</span>
      <span style="font-size:11px;color:#3fb950;font-weight:600">SET</span>
    </div>
    <button class="btn-sm btn-warn" style="margin-top:6px" onclick="unlockBuilder()">Edit Builder Keys</button>
  </div>
  <div id="builder-form">
    <label>Builder API Key</label>
    <input type="password" id="builder-key" placeholder="from polymarket.com/settings?tab=builder">
    <label>Builder Secret</label>
    <input type="password" id="builder-secret" placeholder="secret">
    <label>Builder Passphrase</label>
    <input type="password" id="builder-pass" placeholder="passphrase">
    <button class="btn-primary btn-sm" style="margin-top:8px" onclick="saveBuilder()">Save Builder Keys</button>
    <div id="builder-status" style="font-size:11px;color:#8b949e;margin-top:4px"></div>
  </div>
</div>

<!-- Order Book (5 levels) -->
<div class="card full">
  <h2>Order Book <span style="font-size:10px;color:#484f58">(5 levels, 500ms refresh)</span></h2>
  <div class="row" style="gap:16px">
    <div style="flex:1">
      <div style="font-size:12px;font-weight:600;color:#58a6ff;margin-bottom:4px" id="book-a-title">Team A</div>
      <table>
        <thead><tr><th>Bid Sz</th><th>Bid</th><th>Ask</th><th>Ask Sz</th></tr></thead>
        <tbody id="book-a-body"></tbody>
      </table>
    </div>
    <div style="flex:1">
      <div style="font-size:12px;font-weight:600;color:#58a6ff;margin-bottom:4px" id="book-b-title">Team B</div>
      <table>
        <thead><tr><th>Bid Sz</th><th>Bid</th><th>Ask</th><th>Ask Sz</th></tr></thead>
        <tbody id="book-b-body"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Manual Trade -->
<div class="card full">
  <h2>Trade <span style="font-size:10px;color:#484f58">(GTC limit orders)</span></h2>
  <div class="row" style="align-items:end">
    <div>
      <label>Token</label>
      <select id="trade-team">
        <option value="A" id="trade-team-a">Team A</option>
        <option value="B" id="trade-team-b">Team B</option>
      </select>
    </div>
    <div>
      <label>Side</label>
      <select id="trade-side">
        <option value="BUY">BUY</option>
        <option value="SELL">SELL</option>
      </select>
    </div>
    <div>
      <label>Price</label>
      <input type="text" id="trade-price" placeholder="0.55">
    </div>
    <div>
      <label>Size (tokens)</label>
      <input type="number" id="trade-size" placeholder="100">
    </div>
    <div>
      <button class="btn-primary" onclick="placeTrade()">Place Order</button>
    </div>
  </div>
  <div id="trade-msg" style="font-size:12px;margin-top:6px;color:#8b949e"></div>
</div>

<!-- Sweep Controls -->
<div class="card full">
  <h2>Sweep Controls</h2>
  <div class="row" style="align-items:end">
    <div>
      <label>Winning Team</label>
      <select id="sweep-winner">
        <option value="A" id="winner-opt-a">Team A</option>
        <option value="B" id="winner-opt-b">Team B</option>
      </select>
    </div>
    <div>
      <label>Budget (USDC)</label>
      <input type="number" id="sweep-budget" value="50" placeholder="50">
    </div>
    <div>
      <label>Dry Run</label>
      <select id="sweep-dry">
        <option value="true" selected>Yes (safe)</option>
        <option value="false">No (LIVE)</option>
      </select>
    </div>
    <div>
      <label style="display:flex;align-items:center;gap:4px;margin-top:0">
        <input type="checkbox" id="sweep-absolute" style="width:auto">
        <span>Absolute (0.995-0.999)</span>
      </label>
      <div style="font-size:10px;color:#484f58">Default: book-relative (2nd level + 4 ticks)</div>
    </div>
  </div>
  <div id="sweep-preview" style="font-size:11px;color:#8b949e;margin-top:8px;font-family:monospace"></div>
  <div class="row" style="margin-top:12px">
    <button class="btn-primary" id="btn-sweep-start" onclick="startSweep()">Start Sweep</button>
    <button class="btn-danger" id="btn-sweep-stop" onclick="stopSweep()" disabled>Stop Sweep</button>
    <button class="btn-danger" onclick="cancelAll()" style="background:#8b2500">Cancel ALL Orders</button>
  </div>
  <div id="sweep-msg" style="font-size:12px;color:#d29922;margin-top:8px"></div>
  <div id="sweep-orders-info" style="font-size:11px;color:#8b949e;margin-top:4px"></div>
</div>

<!-- Events Log -->
<div class="card full">
  <h2>Events</h2>
  <div class="events" id="events"></div>
</div>

</div><!-- /grid -->

<script>
const API = '';

async function api(path, opts) {
  try {
    const r = await fetch(API + path, opts);
    const text = await r.text();
    try { return JSON.parse(text); }
    catch { return r.ok ? null : {ok:false, error: text}; }
  } catch(e) {
    console.error(path, e);
    return {ok:false, error: e.message};
  }
}

async function saveWallet() {
  const pk = document.getElementById('pk').value.trim();
  if (!pk) {
    document.getElementById('wallet-status').textContent = 'Private key required';
    document.getElementById('wallet-status').style.color = '#da3633';
    return;
  }
  const sig = parseInt(document.getElementById('sig-type').value);
  document.getElementById('wallet-status').textContent = 'Deriving addresses + API key...';
  const r = await api('/api/wallet', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({private_key: pk, signature_type: sig})
  });
  if (r?.ok) {
    document.getElementById('wallet-status').textContent = 'OK';
    lockWallet(r.eoa_address, r.proxy_address, sig);
  } else {
    document.getElementById('wallet-status').textContent = r?.error || 'Error';
    document.getElementById('wallet-status').style.color = '#da3633';
  }
  pollBalances();
}

function lockWallet(eoa, proxy, sig) {
  document.getElementById('wallet-eoa-display').textContent = eoa || '(not derived)';
  document.getElementById('wallet-proxy-display').textContent = proxy || '—';
  document.getElementById('wallet-sig-display').textContent = 'sig=' + sig;
  document.getElementById('wallet-locked').style.display = '';
  document.getElementById('wallet-form').style.display = 'none';
}

function unlockWallet() {
  document.getElementById('wallet-locked').style.display = 'none';
  document.getElementById('wallet-form').style.display = '';
  document.getElementById('wallet-status').textContent = '';
}

async function fetchMarket() {
  const slug = document.getElementById('slug').value.trim();
  if (!slug) return;
  document.getElementById('market-info').textContent = 'Fetching...';
  const r = await api('/api/fetch-market', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({slug})
  });
  if (r?.ok) {
    document.getElementById('market-info').innerHTML =
      `<strong>${r.team_a_name}</strong> vs <strong>${r.team_b_name}</strong><br>` +
      `tick=${r.tick_size} min_size=${r.order_min_size} neg_risk=${r.neg_risk}` +
      (r.restricted ? '<br><span style="color:#da3633">RESTRICTED MARKET</span>' : '');
    document.getElementById('winner-opt-a').textContent = r.team_a_name;
    document.getElementById('winner-opt-b').textContent = r.team_b_name;
    document.getElementById('book-a-title').textContent = r.team_a_name;
    document.getElementById('book-b-title').textContent = r.team_b_name;
    document.getElementById('token-a-label').textContent = r.team_a_name;
    document.getElementById('token-b-label').textContent = r.team_b_name;
  } else {
    document.getElementById('market-info').textContent = 'Error: ' + JSON.stringify(r);
  }
}

async function pollBalances() {
  const r = await api('/api/sweep/balances');
  if (!r) return;
  document.getElementById('eoa-addr').textContent = r.eoa_address || '—';
  document.getElementById('proxy-addr-display').textContent = r.proxy_address || '—';
  document.getElementById('eoa-usdc').textContent = r.eoa_usdc || '—';
  document.getElementById('proxy-usdc').textContent = r.proxy_usdc || '—';
  // EOA tokens (after split, before move)
  document.getElementById('eoa-token-a').textContent = r.eoa_team_a_tokens || '0';
  document.getElementById('eoa-token-b').textContent = r.eoa_team_b_tokens || '0';
  // Proxy tokens (available for CLOB)
  document.getElementById('proxy-token-a').textContent = r.proxy_team_a_tokens || '0';
  document.getElementById('proxy-token-b').textContent = r.proxy_team_b_tokens || '0';
  if (r.team_a_name) {
    const setLabel = (id, name) => { const el = document.getElementById(id); if(el) el.textContent = name; };
    setLabel('token-a-label', r.team_a_name);
    setLabel('token-b-label', r.team_b_name);
    setLabel('token-a-label-p', r.team_a_name);
    setLabel('token-b-label-p', r.team_b_name);
    setLabel('winner-opt-a', r.team_a_name);
    setLabel('winner-opt-b', r.team_b_name);
  }
  if (r.tick_size) {
    document.getElementById('tick-display').textContent = 'Live tick size: ' + r.tick_size;
  }
}

async function pollBook() {
  const r = await api('/api/book');
  if (!r) return;
  const render = (bids, asks, tbodyId) => {
    const body = document.getElementById(tbodyId);
    const rows = [];
    const N = 5;
    for (let i = 0; i < N; i++) {
      const b = bids[i];
      const a = asks[i];
      rows.push(`<tr>
        <td class="bid">${b ? parseFloat(b.size).toFixed(0) : ''}</td>
        <td class="bid">${b ? b.price : ''}</td>
        <td class="ask">${a ? a.price : ''}</td>
        <td class="ask">${a ? parseFloat(a.size).toFixed(0) : ''}</td>
      </tr>`);
    }
    body.innerHTML = rows.join('');
  };
  render(r.team_a_bids || [], r.team_a_asks || [], 'book-a-body');
  render(r.team_b_bids || [], r.team_b_asks || [], 'book-b-body');
  if (r.team_a_name) {
    document.getElementById('book-a-title').textContent = r.team_a_name;
    document.getElementById('book-b-title').textContent = r.team_b_name;
  }
}

async function pollSweepStatus() {
  const r = await api('/api/sweep/status');
  if (!r) return;
  const badge = document.getElementById('sweep-badge');
  const dryBadge = document.getElementById('dry-badge');
  const btnStart = document.getElementById('btn-sweep-start');
  const btnStop = document.getElementById('btn-sweep-stop');
  const info = document.getElementById('sweep-orders-info');
  if (r.phase === 'active') {
    badge.className = 'badge badge-active';
    badge.textContent = 'ACTIVE';
    btnStart.disabled = true;
    btnStop.disabled = false;
    dryBadge.style.display = r.dry_run ? '' : 'none';
    info.textContent = `Resting orders: ${r.resting_orders} | Budget: $${r.budget}`;
  } else {
    badge.className = 'badge badge-idle';
    badge.textContent = 'IDLE';
    dryBadge.style.display = 'none';
    btnStart.disabled = false;
    btnStop.disabled = true;
    info.textContent = '';
  }
}

async function pollEvents() {
  const r = await api('/api/events');
  if (!r || !Array.isArray(r)) return;
  const el = document.getElementById('events');
  el.innerHTML = r.slice(-50).reverse().map(e =>
    `<div class="ev"><span class="ev-ts">${e.ts}</span><span class="ev-kind">${e.kind}</span><span class="ev-detail">${e.detail}</span></div>`
  ).join('');
}

async function startSweep() {
  const msg = document.getElementById('sweep-msg');
  const dry = document.getElementById('sweep-dry').value === 'true';
  const absolute = document.getElementById('sweep-absolute').checked;
  if (!dry && !confirm('LIVE MODE — real money will be used. Continue?')) return;
  msg.textContent = 'Placing 10 GTC orders...';
  msg.style.color = '#8b949e';
  const r = await api('/api/sweep/start', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      winning_team: document.getElementById('sweep-winner').value,
      budget_usdc: document.getElementById('sweep-budget').value,
      dry_run: dry,
      absolute: absolute,
    })
  });
  if (r?.ok) {
    const info = `${r.mode} | ${r.orders} orders` + (r.batch_ms ? ` | ${r.batch_ms}ms` : '');
    msg.textContent = 'Sweep placed: ' + info;
    msg.style.color = '#3fb950';
    if (r.buy_prices) {
      document.getElementById('sweep-preview').textContent =
        'BUY @ [' + r.buy_prices.join(', ') + '] | SELL @ [' + r.sell_prices.join(', ') + ']';
    }
  } else {
    msg.textContent = r?.error || JSON.stringify(r) || 'Error';
    msg.style.color = '#da3633';
  }
}

async function stopSweep() {
  const r = await api('/api/sweep/stop', {method:'POST'});
  document.getElementById('sweep-msg').textContent = r?.ok ? 'Sweep stopped' : 'Error';
}

async function cancelAll() {
  if (!confirm('Cancel ALL open orders?')) return;
  await api('/api/cancel-all', {method:'POST'});
}

async function placeTrade() {
  const msg = document.getElementById('trade-msg');
  const team = document.getElementById('trade-team').value;
  const side = document.getElementById('trade-side').value;
  const price = document.getElementById('trade-price').value.trim();
  const size = document.getElementById('trade-size').value.trim();
  if (!price || !size) { msg.textContent = 'Price and size required'; msg.style.color = '#da3633'; return; }
  msg.textContent = 'Placing...'; msg.style.color = '#8b949e';
  const r = await api('/api/trade', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({team, side, price, size})
  });
  if (r?.ok) {
    msg.textContent = `${side} placed: ${r.order_id} (${r.latency_ms}ms)`;
    msg.style.color = '#3fb950';
  } else {
    msg.textContent = r?.error || JSON.stringify(r) || 'Error';
    msg.style.color = '#da3633';
  }
}

async function refreshTick() {
  const r = await api('/api/refresh-tick', {method:'POST'});
  if (r?.ok) {
    document.getElementById('tick-display').textContent = 'Live tick: ' + r.tick_size + (r.changed ? ' (CHANGED!)' : '');
    document.getElementById('tick-display').style.color = r.changed ? '#d29922' : '#484f58';
  }
}

async function moveTokens(dir) {
  const r = await api('/api/move-tokens', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({direction: dir})
  });
  if (r?.ok) pollBalances();
  else alert('Move failed: ' + JSON.stringify(r));
}

async function moveUsdc(dir) {
  const amt = prompt('Amount USDC to move:');
  if (!amt) return;
  const r = await api('/api/move-usdc', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({amount_usdc: parseInt(amt), direction: dir})
  });
  if (r?.ok) pollBalances();
  else alert('Move failed: ' + JSON.stringify(r));
}

async function doSplit() {
  const amt = parseInt(document.getElementById('split-amount').value);
  if (!amt) return;
  document.getElementById('split-status').textContent = 'Splitting... (on-chain tx, may take 30s)';
  const r = await api('/api/ctf-split', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({amount_usdc: amt})
  });
  document.getElementById('split-status').textContent = r?.ok ? 'Split done! tx=' + r.tx_hash : 'Error: ' + JSON.stringify(r);
  if (r?.ok) pollBalances();
}

async function saveBuilder() {
  const key = document.getElementById('builder-key').value.trim();
  const secret = document.getElementById('builder-secret').value.trim();
  const pass = document.getElementById('builder-pass').value.trim();
  if (!key || !secret || !pass) {
    document.getElementById('builder-status').textContent = 'All 3 fields required';
    document.getElementById('builder-status').style.color = '#da3633';
    return;
  }
  const r = await api('/api/sweep/builder', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      builder_api_key: key,
      builder_api_secret: secret,
      builder_api_passphrase: pass,
    })
  });
  if (r?.ok) {
    document.getElementById('builder-status').textContent = 'Builder keys saved';
    document.getElementById('builder-status').style.color = '#3fb950';
    lockBuilder(key);
  } else {
    document.getElementById('builder-status').textContent = r?.error || 'Error';
    document.getElementById('builder-status').style.color = '#da3633';
  }
}

function lockBuilder(keyOrMasked) {
  const masked = keyOrMasked.length > 12 ? keyOrMasked.slice(0,8) + '...' + keyOrMasked.slice(-4) : keyOrMasked;
  document.getElementById('builder-key-display').textContent = masked;
  document.getElementById('builder-locked').style.display = '';
  document.getElementById('builder-form').style.display = 'none';
}

function unlockBuilder() {
  // Don't clear existing values — they're still in the inputs from last save
  document.getElementById('builder-locked').style.display = 'none';
  document.getElementById('builder-form').style.display = '';
  document.getElementById('builder-status').textContent = '';
}

// Load saved config on page load
async function loadConfig() {
  const r = await api('/api/config');
  if (!r) return;
  if (r.polymarket_address) document.getElementById('proxy-addr').value = r.polymarket_address;
  if (r.signature_type !== undefined) document.getElementById('sig-type').value = r.signature_type;
  if (r.market_slug) document.getElementById('slug').value = r.market_slug;

  // Set team names everywhere
  function setTeamNames(a, b) {
    const set = (id, v) => { const el = document.getElementById(id); if(el) el.textContent = v; };
    set('winner-opt-a', a); set('winner-opt-b', b);
    set('book-a-title', a); set('book-b-title', b);
    set('token-a-label', a); set('token-b-label', b);
    set('token-a-label-p', a); set('token-b-label-p', b);
    set('trade-team-a', a); set('trade-team-b', b);
  }
  if (r.team_a_name && r.team_a_name !== 'TEAM_A') {
    setTeamNames(r.team_a_name, r.team_b_name);
    document.getElementById('market-info').innerHTML =
      `<strong>${r.team_a_name}</strong> vs <strong>${r.team_b_name}</strong>`;
  }

  // Lock wallet if already configured
  if (r.wallet_set || r.private_key_set) {
    lockWallet(r.eoa_address || '', r.polymarket_address || '', r.signature_type || 1);
  }

  // Lock builder keys if already set
  if (r.builder_key_set && r.builder_api_key_masked) {
    lockBuilder(r.builder_api_key_masked);
  }
}

loadConfig();
pollBalances();
pollBook();
pollSweepStatus();
pollEvents();

setInterval(pollBalances, 10000);
setInterval(pollBook, 500);
setInterval(pollSweepStatus, 2000);
setInterval(pollEvents, 1500);
</script>
</body>
</html>
"##;

pub const TAKER_HTML: &str = r##"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TOTEM TAKER</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono',Menlo,Monaco,monospace;background:#0a0a0f;color:#c9d1d9;min-height:100vh;padding:12px;max-width:1200px;margin:0 auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #1b1f27;margin-bottom:12px}
.header h1{font-size:16px;color:#58a6ff;font-weight:700;letter-spacing:2px}
.phase{padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;text-transform:uppercase}
.phase-idle{background:#30363d;color:#8b949e}
.phase-running{background:#238636;color:#fff}
.phase-dry{background:#6e40c9;color:#fff}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.card{background:#111318;border:1px solid #1b1f27;border-radius:6px;padding:10px}
.card h3{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.val{font-size:18px;font-weight:700;color:#e6edf3}
.val-sm{font-size:13px;color:#c9d1d9}
.pnl-pos{color:#3fb950}.pnl-neg{color:#f85149}.pnl-zero{color:#8b949e}
.sig-btns{display:flex;gap:4px;flex-wrap:wrap}
.sig-btn{padding:6px 14px;border:1px solid #30363d;background:#161b22;color:#c9d1d9;border-radius:4px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
.sig-btn:hover{border-color:#58a6ff;color:#58a6ff}
.sig-btn.w{border-color:#f85149;color:#f85149}.sig-btn.w:hover{background:#f8514922}
.sig-btn.b{border-color:#3fb950;color:#3fb950}.sig-btn.b:hover{background:#3fb95022}
.sig-btn.io{border-color:#d29922;color:#d29922}
.sig-btn.danger{border-color:#f85149;color:#f85149}
.sig-btn.action{border-color:#238636;color:#238636}
.sig-btn.action:hover{background:#23863622}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:5px 8px;color:#8b949e;font-weight:600;font-size:10px;text-transform:uppercase;border-bottom:1px solid #1b1f27}
td{padding:4px 8px;border-bottom:1px solid #0d1117}
tr:hover{background:#161b22}
.buy{color:#3fb950}.sell{color:#f85149}
.fak{color:#d29922}.gtc{color:#58a6ff}
.section{margin-bottom:12px}
.section h2{font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.book-row{display:flex;justify-content:space-between;padding:3px 0;font-size:13px}
.book-label{color:#8b949e;font-size:11px}
.lat-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:6px}
.lat-card{background:#111318;border:1px solid #1b1f27;border-radius:4px;padding:5px;text-align:center}
.lat-card .name{font-size:9px;color:#8b949e;text-transform:uppercase}
.lat-card .p50{font-size:13px;font-weight:700;color:#58a6ff}
.lat-card .detail{font-size:9px;color:#484f58}
.event-log{max-height:180px;overflow-y:auto;font-size:11px;background:#0d1117;border:1px solid #1b1f27;border-radius:4px;padding:6px}
.event-line{padding:2px 0;border-bottom:1px solid #0d1017}
.event-ts{color:#484f58;margin-right:6px}
.event-kind{font-weight:600;margin-right:6px}
.event-kind.filled{color:#3fb950}.event-kind.error{color:#f85149}.event-kind.warn{color:#d29922}
.event-kind.trade{color:#58a6ff}.event-kind.tg-signal{color:#bc8cff}.event-kind.pnl{color:#3fb950}
.empty{color:#484f58;text-align:center;padding:12px}
.flash{animation:flash-bg 0.6s ease-out}
@keyframes flash-bg{0%{background:#58a6ff33}100%{background:transparent}}
label{font-size:11px;color:#8b949e;display:block;margin-bottom:2px;margin-top:6px}
input{background:#0d1117;border:1px solid #30363d;color:#e1e4e8;padding:4px 8px;border-radius:4px;font-size:12px;width:100%;font-family:inherit}
</style>
</head>
<body>
<div class="header">
  <h1>TOTEM TAKER</h1>
  <div>
    <span id="rt-pnl" class="pnl-zero" style="font-size:14px;font-weight:700;margin-right:12px">PnL: $0.00</span>
    <span id="phase" class="phase phase-idle">IDLE</span>
    <span id="innings" style="color:#8b949e;font-size:12px;margin-left:8px"></span>
  </div>
</div>

<!-- ROW 1: Order Book + Signals -->
<div class="row">
  <div class="card">
    <h3>Order Book</h3>
    <div id="book-a" class="book-row"></div>
    <div id="book-b" class="book-row" style="margin-top:4px"></div>
  </div>
  <div class="card">
    <h3>Signal</h3>
    <div class="sig-btns">
      <button class="sig-btn" onclick="sig('0')">0</button>
      <button class="sig-btn" onclick="sig('1')">1</button>
      <button class="sig-btn" onclick="sig('2')">2</button>
      <button class="sig-btn" onclick="sig('3')">3</button>
      <button class="sig-btn b" onclick="sig('4')">4</button>
      <button class="sig-btn b" onclick="sig('6')">6</button>
      <button class="sig-btn w" onclick="sig('W')">W</button>
      <button class="sig-btn" onclick="sig('Wd')">Wd</button>
      <button class="sig-btn" onclick="sig('N')">N</button>
    </div>
  </div>
</div>

<!-- ROW 2: Positions + Settings/Controls -->
<div class="row">
  <div class="card">
    <h3>Position</h3>
    <div class="val-sm" id="positions"></div>
    <div style="margin-top:6px">
      <span class="book-label">Budget: </span><span id="budget" class="val-sm"></span>
      <span class="book-label" style="margin-left:12px">Spent: </span><span id="spent" class="val-sm"></span>
      <span class="book-label" style="margin-left:12px">Trades: </span><span id="trade-count" class="val-sm"></span>
    </div>
    <div style="margin-top:4px">
      <span class="book-label">Unrealized: </span><span id="upnl" class="val-sm pnl-zero"></span>
      <span class="book-label" style="margin-left:12px">Round-Trip: </span><span id="rt-pnl-detail" class="val-sm pnl-zero"></span>
    </div>
  </div>
  <div class="card">
    <h3>Settings</h3>
    <div id="settings-info" class="val-sm"></div>
    <div style="margin-top:8px" class="sig-btns">
      <button class="sig-btn action" onclick="startInnings()">Start Innings</button>
      <button class="sig-btn io" onclick="sig('IO')">Stop Innings</button>
      <button class="sig-btn danger" onclick="sig('MO')">Match Over</button>
      <button class="sig-btn" onclick="cancelAll()">Cancel All</button>
    </div>
  </div>
</div>

<!-- ROW 3: Filled Orders + Open/Pending Orders side by side -->
<div class="row">
  <div class="section">
    <h2>Filled / Closed Orders</h2>
    <div style="max-height:300px;overflow-y:auto">
    <table>
      <thead><tr><th>Time</th><th>Signal</th><th>Side</th><th>Team</th><th>Size</th><th>Price</th><th>Cost</th><th>Type</th></tr></thead>
      <tbody id="trades"></tbody>
    </table>
    </div>
  </div>
  <div class="section">
    <h2>Open / Pending Orders</h2>
    <div style="max-height:300px;overflow-y:auto">
    <table>
      <thead><tr><th>Side</th><th>Team</th><th>Price</th><th>Size</th><th>Filled</th><th>Status</th><th></th></tr></thead>
      <tbody id="open-orders"></tbody>
    </table>
    <div style="margin-top:8px">
      <span style="color:#8b949e;font-size:10px;text-transform:uppercase">Pending Reverts</span>
    </div>
    <table>
      <thead><tr><th>Label</th><th>Side</th><th>Team</th><th>Entry</th><th>Limit</th><th>Age</th><th></th></tr></thead>
      <tbody id="reverts"></tbody>
    </table>
    </div>
  </div>
</div>

<!-- Latency -->
<div class="section">
  <h2>Latency</h2>
  <div class="lat-grid" id="latency"></div>
</div>

<!-- Event Log -->
<div class="section">
  <h2>Event Log</h2>
  <div class="event-log" id="events"></div>
</div>

<script>
let prevTradeCount = 0;
function esc(s) { if (s == null) return ''; const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }

async function poll() {
  try {
    const r = await fetch('/api/taker/status');
    if (!r.ok) return;
    const text = await r.text();
    if (!text) return;
    const d = JSON.parse(text);
    render(d);
  } catch(e) { console.error('poll', e); }
}

function render(d) {
  // Header
  const phaseEl = document.getElementById('phase');
  phaseEl.textContent = d.dry_run ? 'DRY RUN' : (d.phase||'').replace('_',' ').toUpperCase();
  phaseEl.className = 'phase ' + (d.dry_run ? 'phase-dry' : d.phase === 'innings_running' ? 'phase-running' : 'phase-idle');
  document.getElementById('innings').textContent = `Inn ${d.innings} | ${esc(d.batting)} batting`;

  // PnL header
  const rtPnl = parseFloat(d.round_trip_pnl) || 0;
  const rtEl = document.getElementById('rt-pnl');
  rtEl.textContent = `PnL: $${rtPnl.toFixed(4)}`;
  rtEl.className = rtPnl > 0 ? 'pnl-pos' : rtPnl < 0 ? 'pnl-neg' : 'pnl-zero';
  rtEl.style.cssText = 'font-size:14px;font-weight:700;margin-right:12px';

  // Book
  document.getElementById('book-a').innerHTML = `<span class="book-label">${esc(d.team_a_name)}</span><span><span class="buy">${d.book_a_bid||'—'}</span> / <span class="sell">${d.book_a_ask||'—'}</span></span>`;
  document.getElementById('book-b').innerHTML = `<span class="book-label">${esc(d.team_b_name)}</span><span><span class="buy">${d.book_b_bid||'—'}</span> / <span class="sell">${d.book_b_ask||'—'}</span></span>`;

  // Position
  document.getElementById('positions').textContent = `${d.team_a_name}: ${d.team_a_tokens} tokens | ${d.team_b_name}: ${d.team_b_tokens} tokens`;
  document.getElementById('budget').textContent = `$${parseFloat(d.remaining).toFixed(2)} / ${parseFloat(d.total_budget).toFixed(2)}`;
  document.getElementById('spent').textContent = `$${parseFloat(d.total_spent).toFixed(2)}`;
  document.getElementById('trade-count').textContent = d.trade_count;

  const upnl = parseFloat(d.unrealized_pnl) || 0;
  const upnlEl = document.getElementById('upnl');
  upnlEl.textContent = `$${upnl.toFixed(4)}`;
  upnlEl.className = 'val-sm ' + (upnl > 0 ? 'pnl-pos' : upnl < 0 ? 'pnl-neg' : 'pnl-zero');

  const rtDetail = document.getElementById('rt-pnl-detail');
  rtDetail.textContent = `$${rtPnl.toFixed(4)}`;
  rtDetail.className = 'val-sm ' + (rtPnl > 0 ? 'pnl-pos' : rtPnl < 0 ? 'pnl-neg' : 'pnl-zero');

  // Settings
  document.getElementById('settings-info').innerHTML =
    `slug: <strong>${esc(d.market_slug||'—')}</strong> | max_trade: $${d.max_trade_usdc} | edge W:${d.edge_wicket} 4:${d.edge_boundary_4} 6:${d.edge_boundary_6} | revert_delay: ${d.revert_delay_ms}ms`;

  // Filled orders (trades)
  const tbody = document.getElementById('trades');
  if (d.trades && d.trades.length !== prevTradeCount) {
    prevTradeCount = d.trades.length;
    if (d.trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No trades yet</td></tr>';
    } else {
      tbody.innerHTML = d.trades.slice().reverse().map(t =>
        `<tr class="flash">
          <td>${esc(t.ts)}</td><td>${esc(t.label)}</td>
          <td class="${t.side==='BUY'?'buy':'sell'}">${esc(t.side)}</td>
          <td>${esc(t.team)}</td><td>${esc(t.size)}</td><td>${esc(t.price)}</td>
          <td>$${parseFloat(t.cost).toFixed(2)}</td>
          <td class="${t.order_type==='FAK'?'fak':'gtc'}">${esc(t.order_type)}</td>
        </tr>`
      ).join('');
    }
  }

  // Open orders
  const openEl = document.getElementById('open-orders');
  const allOpen = [...(d.open_orders||[]).map(o => ({...o, src:'clob'})), ...(d.pending_reverts||[]).map(r => ({side:r.side, team:r.team, price:r.revert_limit, original_size:r.size, size_matched:'0', status:'revert', order_id:r.order_id, src:'revert'}))];
  if (allOpen.length === 0) {
    openEl.innerHTML = '<tr><td colspan="7" class="empty">No open orders</td></tr>';
  } else {
    openEl.innerHTML = allOpen.filter(o=>o.src==='clob').map(o =>
      `<tr>
        <td class="${o.side==='BUY'?'buy':'sell'}">${esc(o.side)}</td>
        <td>${esc(o.team)}</td><td>${esc(o.price)}</td><td>${esc(o.original_size)}</td>
        <td>${esc(o.size_matched)}</td><td>${esc(o.status)}</td>
        <td><button class="sig-btn" style="padding:1px 6px;font-size:10px" onclick="cancelOrder('${esc(o.order_id)}')">x</button></td>
      </tr>`
    ).join('');
  }

  // Pending reverts
  const revEl = document.getElementById('reverts');
  if (!d.pending_reverts || d.pending_reverts.length === 0) {
    revEl.innerHTML = '<tr><td colspan="7" class="empty">No pending reverts</td></tr>';
  } else {
    revEl.innerHTML = d.pending_reverts.map(r =>
      `<tr>
        <td>${esc(r.label)}</td>
        <td class="${r.side==='BUY'?'buy':'sell'}">${esc(r.side)}</td>
        <td>${esc(r.team)}</td><td>${esc(r.entry_price)}</td><td>${esc(r.revert_limit)}</td>
        <td>${r.age_secs.toFixed(0)}s</td>
        <td><button class="sig-btn" style="padding:1px 6px;font-size:10px" onclick="cancelOrder('${esc(r.order_id)}')">x</button></td>
      </tr>`
    ).join('');
  }

  // Latency
  const lat = d.latency;
  if (lat) {
    document.getElementById('latency').innerHTML = [
      {name:'sig→decision', data:lat.signal_to_decision},
      {name:'sign→post', data:lat.sign_to_post},
      {name:'post→resp', data:lat.post_to_response},
      {name:'fill (ws)', data:lat.fill_detect_ws},
      {name:'fill (poll)', data:lat.fill_detect_poll},
      {name:'e2e', data:lat.e2e_signal_to_fill},
    ].map(m => {
      const p50 = m.data.count > 0 ? (m.data.p50_us/1000).toFixed(1)+'ms' : '—';
      const p95 = m.data.count > 0 ? (m.data.p95_us/1000).toFixed(1) : '—';
      return `<div class="lat-card"><div class="name">${m.name}</div><div class="p50">${p50}</div><div class="detail">p95:${p95}ms n:${m.data.count}</div></div>`;
    }).join('');
  }

  // Events
  const evEl = document.getElementById('events');
  if (d.events) {
    evEl.innerHTML = d.events.map(e =>
      `<div class="event-line"><span class="event-ts">${esc(e.ts)}</span><span class="event-kind ${esc(e.kind)}">${esc(e.kind)}</span> ${esc(e.detail)}</div>`
    ).join('');
  }
}

async function sig(s) {
  try {
    let url = '/api/signal', opts = {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({signal:s})};
    if (s === 'IO') { url = '/api/stop-innings'; opts = {method:'POST'}; }
    else if (s === 'MO') { url = '/api/match-over'; opts = {method:'POST'}; }
    await fetch(url, opts);
    setTimeout(poll, 300);
  } catch(e) { console.error('signal', e); }
}

async function startInnings() {
  try { await fetch('/api/start-innings', {method:'POST'}); setTimeout(poll, 500); } catch(e) { console.error(e); }
}

async function cancelAll() {
  if (!confirm('Cancel ALL open orders?')) return;
  try { await fetch('/api/cancel-all', {method:'POST'}); setTimeout(poll, 500); } catch(e) { console.error(e); }
}

async function cancelOrder(oid) {
  try {
    await fetch('/api/taker/cancel-order', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({order_id: oid})});
    setTimeout(poll, 500);
  } catch(e) { console.error(e); }
}

poll();
setInterval(poll, 1500);
</script>
</body>
</html>
"##;
