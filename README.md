# totem-mm

Betfair + Polymarket RFQ Market Maker (dry run by default).

## Overview

**totem-mm** is a market-making bot that:
- Uses **Betfair** as the reference for prices (odds)
- Quotes on **Polymarket** via **RFQ (Request For Quote)**
- Maps Polymarket tokens to Betfair selection IDs via **TOKEN_MAP**
- Can run in **polling** or **streaming** mode for both Betfair and Polymarket
- Runs in **DRY_RUN** by default (log only, no real trades)

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -e .
   ```

2. **Configure environment:**
   ```bash
   cp env.example .env
   # Edit .env with your credentials
   ```

3. **Choose your mode** (in `main.py`):
   ```python
   USE_BETFAIR_STREAMING = True   # Set to False for polling
   USE_POLYMARKET_STREAMING = True  # Set to False for polling
   ```

4. **Run:**
   ```bash
   python main.py
   ```

---

## Enabling Streaming Mode

### Betfair Streaming

**Step 1: Set mode in `main.py`**
```python
USE_BETFAIR_STREAMING = True
```

**Step 2: Configure credentials in `.env`**
```bash
BETFAIR_USERNAME=your_username
BETFAIR_PASSWORD=your_password
BETFAIR_APP_KEY=your_app_key
```

**Step 3: Set up SSL certificates**

1. **Create self-signed certificate:**
   ```bash
   # Create openssl.cnf with client-auth extensions (see docs/BETFAIR_CERTIFICATE_SETUP.md)
   openssl genrsa -out client-2048.key 2048
   openssl req -new -key client-2048.key -out client-2048.csr -subj "/CN=Betfair API"
   openssl x509 -req -days 365 -in client-2048.csr -signkey client-2048.key \
     -out client-2048.crt -extfile openssl.cnf -extensions ssl_client
   ```

2. **Upload certificate to Betfair:**
   - Go to: https://myaccount.betfair.com/accountdetails/mysecurity?showAPI=1
   - Find "Automated Betting Program Access" → Edit
   - Upload `client-2048.crt` (the `.crt` file only, not `.key`)

3. **Place certificates in project:**
   ```bash
   mkdir -p certs
   cp client-2048.crt certs/
   cp client-2048.key certs/
   ```
   
   Or set in `.env`:
   ```bash
   BETFAIR_CERTS=./certs  # Directory with .crt and .key
   # OR
   BETFAIR_CERT_FILE=/path/to/client-2048.pem  # Single .pem file
   ```

**Step 4: Install streaming dependencies**
```bash
pip install betfairlightweight>=2.20.0
```

**What you get:**
- WebSocket connection to Betfair streaming API
- < 200ms latency price updates
- Real-time market data

---

### Polymarket Streaming

**Step 1: Set mode in `main.py`**
```python
USE_POLYMARKET_STREAMING = True
```

**Step 2: Configure credentials in `.env`**
```bash
POLYMARKET_ADDRESS=0x...  # Polygon wallet address
POLYMARKET_PRIVATE_KEY=...  # Private key (hex, with or without 0x)
POLYMARKET_SIGNATURE_TYPE=1  # 0=EOA, 1=POLY_PROXY, 2=GNOSIS_SAFE
```

**Step 3: Install streaming dependencies**
```bash
pip install websockets>=12.0
```

**What you get:**
- WebSocket connection to RTDS (`wss://ws-live-data.polymarket.com`)
- Real-time RFQ events (< 100ms latency)
- Subscribes to `rfq` topic automatically

---

## Polling Mode (Fallback)

If you set `USE_BETFAIR_STREAMING = False` or `USE_POLYMARKET_STREAMING = False`, the app uses HTTP polling instead.

### Betfair Polling

**Requirements:**
```bash
BETFAIR_APP_KEY=your_app_key
BETFAIR_SESSION_TOKEN=your_session_token  # Expires, needs refresh
```

**How it works:** HTTP polling every 10 seconds (~2000ms latency)

### Polymarket Polling

**Requirements:**
```bash
POLYMARKET_ADDRESS=0x...
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_SIGNATURE_TYPE=1
```

**How it works:** HTTP polling `get_pending_requests` every 5 seconds (configurable via `RFQ_POLL_INTERVAL`)

---

## Configuration Examples

### Both Streaming (Recommended)
```python
# In main.py
USE_BETFAIR_STREAMING = True
USE_POLYMARKET_STREAMING = True
```

```bash
# In .env
BETFAIR_USERNAME=your_username
BETFAIR_PASSWORD=your_password
BETFAIR_APP_KEY=your_app_key
BETFAIR_CERTS=./certs

POLYMARKET_ADDRESS=0x...
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_SIGNATURE_TYPE=1
```

### Both Polling
```python
# In main.py
USE_BETFAIR_STREAMING = False
USE_POLYMARKET_STREAMING = False
```

```bash
# In .env
BETFAIR_APP_KEY=your_app_key
BETFAIR_SESSION_TOKEN=your_token

POLYMARKET_ADDRESS=0x...
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_SIGNATURE_TYPE=1
```

### Mixed (Betfair streaming, Polymarket polling)
```python
# In main.py
USE_BETFAIR_STREAMING = True
USE_POLYMARKET_STREAMING = False
```

```bash
# In .env
BETFAIR_USERNAME=your_username
BETFAIR_PASSWORD=your_password
BETFAIR_APP_KEY=your_app_key
BETFAIR_CERTS=./certs

POLYMARKET_ADDRESS=0x...
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_SIGNATURE_TYPE=1
```

---

## API Endpoints

- `GET /health` - Health check
- `GET /status` - System status (modes, uptime)
- `GET /prices` - Current Betfair prices
- `GET /quotes` - Active RFQ quotes
- `GET /config` - Current configuration

---

## Safety Features

- **DRY_RUN mode** (default: `true`): Logs quotes but doesn't submit them
- **APPROVE_ORDERS** (default: `false`): Never approves orders unless explicitly enabled
- **Exposure limits**: `RFQ_MAX_QUOTE_SIZE_USDC`, `RFQ_MAX_EXPOSURE_USDC`

---

## Troubleshooting

**Betfair streaming not working:**
- Verify `USE_BETFAIR_STREAMING = True` in `main.py`
- Check SSL certificates are in `certs/` or set `BETFAIR_CERTS`/`BETFAIR_CERT_FILE` in `.env`
- Verify certificates are uploaded to Betfair account
- Check logs for connection errors
- Ensure `betfairlightweight>=2.20.0` is installed

**Polymarket streaming not working:**
- Verify `USE_POLYMARKET_STREAMING = True` in `main.py`
- Check RTDS WebSocket connection in logs
- HTTP 429 on first connect is normal (retries automatically)
- Ensure `websockets>=12.0` is installed

**No price updates:**
- Market might be closed/suspended (check Betfair website)
- Verify market ID is correct in `BETFAIR_MARKET_IDS`
- Check logs for `[BETFAIR-STREAMING] Updated odds` messages

**No RFQ opportunities:**
- Normal—RFQs only appear when Polymarket users request quotes
- Verify `TOKEN_MAP` is configured correctly in `.env`
- Check logs for `[STREAMING] RTDS pong received` to confirm stream is alive
- Check logs for `[STREAMING] RTDS rfq message received` when RFQ events occur

---

## See Also

- `docs/BETFAIR_CERTIFICATE_SETUP.md` - Detailed SSL certificate setup guide
- `scripts/check_trade_opportunities.sh` - Quick API check script
- `env.example` - Full environment variable reference
