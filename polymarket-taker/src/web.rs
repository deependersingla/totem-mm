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

<!-- Live Price Chart -->
<div class="card full shared-section" id="chartCard" style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <h2 style="margin-bottom:0">Live Odds</h2>
    <div style="display:flex;gap:12px;align-items:center">
      <select id="chartInterval" onchange="pollPriceChart()" style="background:#0d1117;border:1px solid #30363d;color:#e1e4e8;padding:3px 8px;border-radius:4px;font-size:11px">
        <option value="1m">1 Min</option>
        <option value="5m">5 Min</option>
        <option value="15m">15 Min</option>
        <option value="30m">30 Min</option>
        <option value="1h" selected>1 Hour</option>
        <option value="2h">2 Hours</option>
        <option value="6h">6 Hours</option>
      </select>
      <div id="chartLegend" style="display:flex;gap:16px;font-size:13px;font-weight:600"></div>
    </div>
  </div>
  <canvas id="priceChart" height="200" style="width:100%;background:#0d1117;border-radius:6px"></canvas>
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

</div>

<!-- ======================== MAKER TAB ======================== -->
<div id="tab-maker" class="tab-content" style="display:none">

  <!-- Maker Status Card -->
  <div class="card full">
    <h2>Maker Status</h2>
    <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <span id="mkEnabled" class="badge badge-idle">DISABLED</span>
      <span id="mkDryRunBadge" class="badge badge-dry" style="display:none">DRY RUN</span>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div class="stat"><span>Half Spread</span> <strong id="mkSpread">--</strong></div>
      <div class="stat"><span>Quote Size</span> <strong id="mkQuoteSize">--</strong></div>
      <div class="stat"><span>Inventory Tier</span> <strong id="mkTier">--</strong></div>
    </div>
  </div>

  <!-- Maker Config Card -->
  <div class="card full">
    <h2>Maker Config</h2>
    <div class="row">
      <div><label>Enabled</label>
        <select id="mkCfgEnabled"><option value="false">No</option><option value="true">Yes</option></select>
      </div>
      <div><label>Dry Run <span style="color:#d29922;font-size:10px">(REQUIRED for first use)</span></label>
        <select id="mkCfgDryRun"><option value="true">Yes</option><option value="false">No</option></select>
      </div>
    </div>
    <div class="row">
      <div><label>Half Spread</label><input id="mkCfgSpread" type="text" value="0.02"></div>
      <div><label>Quote Size</label><input id="mkCfgSize" type="text" value="10"></div>
    </div>
    <div class="row">
      <div><label>Use GTD</label>
        <select id="mkCfgGtd"><option value="true">Yes</option><option value="false">No</option></select>
      </div>
      <div><label>GTD Expiry (secs)</label><input id="mkCfgExpiry" type="number" value="30"></div>
    </div>
    <div class="row">
      <div><label>Refresh Interval (secs)</label><input id="mkCfgRefresh" type="number" value="10"></div>
      <div><label>Skew Kappa</label><input id="mkCfgKappa" type="text" value="0.5"></div>
    </div>
    <div class="row">
      <div><label>Max Exposure</label><input id="mkCfgExposure" type="text" value="100"></div>
    </div>
    <div style="border-top:1px solid #21262d;margin-top:10px;padding-top:8px">
      <div style="font-size:12px;color:#8b949e;font-weight:600;margin-bottom:6px">Inventory Tier Thresholds</div>
      <div class="row">
        <div><label>T1 %</label><input id="mkCfgT1" type="number" value="25" step="1"></div>
        <div><label>T2 %</label><input id="mkCfgT2" type="number" value="50" step="1"></div>
        <div><label>T3 %</label><input id="mkCfgT3" type="number" value="75" step="1"></div>
      </div>
    </div>
    <button class="btn-primary" style="margin-top:10px;width:100%" onclick="saveMakerConfig()">Save Maker Config</button>
  </div>

  <!-- Maker Info Box -->
  <div class="card full">
    <h2>How Maker Works</h2>
    <div style="font-size:12px;color:#8b949e;line-height:1.6">
      Maker places 4 resting orders (BUY+SELL on both tokens). On events, dangerous legs are cancelled instantly.
      DRY RUN mode logs quotes without submitting. Enable Dry Run first to verify quotes look correct before going live.
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
  <button class="tab-btn" data-tab="maker" onclick="switchTab('maker')">&#9776; MAKER</button>
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

// Maker polling
async function pollMakerStatus() {
  try {
    const m = await api('/api/maker/status');
    const en = document.getElementById('mkEnabled');
    en.textContent = m.enabled ? 'ENABLED' : 'DISABLED';
    en.className = 'badge ' + (m.enabled ? 'badge-running' : 'badge-idle');
    const dr = document.getElementById('mkDryRunBadge');
    dr.style.display = m.dry_run ? '' : 'none';
    document.getElementById('mkSpread').textContent = m.half_spread != null ? m.half_spread : '--';
    document.getElementById('mkQuoteSize').textContent = m.quote_size != null ? m.quote_size : '--';
    document.getElementById('mkTier').textContent = m.inventory_tier || '--';
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

// Save maker config
async function saveMakerConfig() {
  await api('/api/maker/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
    enabled: document.getElementById('mkCfgEnabled').value === 'true',
    dry_run: document.getElementById('mkCfgDryRun').value === 'true',
    half_spread: document.getElementById('mkCfgSpread').value,
    quote_size: document.getElementById('mkCfgSize').value,
    use_gtd: document.getElementById('mkCfgGtd').value === 'true',
    gtd_expiry_secs: parseInt(document.getElementById('mkCfgExpiry').value),
    refresh_interval_secs: parseInt(document.getElementById('mkCfgRefresh').value),
    skew_kappa: document.getElementById('mkCfgKappa').value,
    max_exposure: document.getElementById('mkCfgExposure').value,
    t1_pct: parseFloat(document.getElementById('mkCfgT1').value),
    t2_pct: parseFloat(document.getElementById('mkCfgT2').value),
    t3_pct: parseFloat(document.getElementById('mkCfgT3').value),
  })});
}

// Load maker config
async function loadMakerConfig() {
  try {
    const c = await api('/api/maker/config');
    if (c.half_spread != null) document.getElementById('mkCfgSpread').value = c.half_spread;
    if (c.quote_size != null) document.getElementById('mkCfgSize').value = c.quote_size;
    if (c.enabled != null) document.getElementById('mkCfgEnabled').value = String(c.enabled);
    if (c.dry_run != null) document.getElementById('mkCfgDryRun').value = String(c.dry_run);
    if (c.use_gtd != null) document.getElementById('mkCfgGtd').value = String(c.use_gtd);
    if (c.gtd_expiry_secs != null) document.getElementById('mkCfgExpiry').value = c.gtd_expiry_secs;
    if (c.refresh_interval_secs != null) document.getElementById('mkCfgRefresh').value = c.refresh_interval_secs;
    if (c.skew_kappa != null) document.getElementById('mkCfgKappa').value = c.skew_kappa;
    if (c.max_exposure != null) document.getElementById('mkCfgExposure').value = c.max_exposure;
    if (c.t1_pct != null) document.getElementById('mkCfgT1').value = c.t1_pct;
    if (c.t2_pct != null) document.getElementById('mkCfgT2').value = c.t2_pct;
    if (c.t3_pct != null) document.getElementById('mkCfgT3').value = c.t3_pct;
  } catch(e) {}
}

// Init
loadConfig();
loadMakerConfig();
pollStatus();
pollEvents();
pollTrades();
pollBook();
pollPriceChart();
pollMakerStatus();
pollLatency();
setInterval(pollStatus, 1500);
setInterval(pollEvents, 1500);
setInterval(pollTrades, 2000);
setInterval(pollBook, 500);
setInterval(pollPriceChart, 1000);
setInterval(pollMakerStatus, 2000);
setInterval(pollLatency, 3000);
</script>
</body>
</html>
"##;
