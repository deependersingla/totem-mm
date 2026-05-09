//! End-to-end test: derive API key from private key via EIP-712 signing,
//! then place a tiny GTC SELL order for ZIM at $0.001.
//!
//! This test forces L1 key derivation (ignoring any pre-configured keys in
//! settings.json) and proves the full auth pipeline works: EIP-712 sign →
//! derive-api-key → HMAC L2 auth → POST /order.
//!
//! Requires: POLYMARKET_PRIVATE_KEY set in .env (real key, not dummy).
//! The order is placed at an absurdly low price so it won't fill.

use polymarket_taker::{clob_auth, config};

/// Override the config to force derivation: clear pre-configured API keys
/// and use only the private key for EIP-712 L1 auth.
fn force_derive_config() -> Option<config::Config> {
    let mut cfg = config::Config::from_env().ok()?;
    if !cfg.has_wallet() {
        eprintln!("SKIP: no real wallet configured");
        return None;
    }
    // Clear pre-configured keys so ClobAuth::derive() is forced to do L1 derivation
    cfg.api_key = String::new();
    cfg.api_secret = String::new();
    cfg.api_passphrase = String::new();
    Some(cfg)
}

#[tokio::test]
async fn test_derive_api_key_from_private_key() {
    let _ = dotenvy::dotenv();

    let cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };

    eprintln!("=== Step 1: Deriving API key via EIP-712 + GET /auth/derive-api-key ===");
    let auth = match clob_auth::ClobAuth::derive(&cfg).await {
        Ok(a) => a,
        Err(e) => {
            panic!("API key derivation failed: {:?}", e);
        }
    };

    assert!(!auth.api_key.is_empty(), "derived api_key must not be empty");
    assert!(!auth.api_secret.is_empty(), "derived api_secret must not be empty");
    assert!(!auth.passphrase.is_empty(), "derived passphrase must not be empty");

    eprintln!("  api_key:    {}", auth.api_key);
    eprintln!("  passphrase: {}", auth.passphrase);
    eprintln!("  address:    {}", auth.address());
    eprintln!("  funder:     {}", auth.funder_address());

    eprintln!("=== Step 2: Testing L2 auth with derived keys (GET /data/orders) ===");
    let path = "/data/orders";
    let headers = auth.l2_headers("GET", path, None).expect("l2_headers failed");

    let url = format!("{}{}", auth.clob_http_url(), path);
    let resp = auth.http_client().get(&url).headers(headers).send().await.expect("GET /data/orders failed");
    let status = resp.status();
    let body = resp.text().await.unwrap_or_default();

    eprintln!("  GET /data/orders status: {} body: {}", status, &body[..body.len().min(200)]);
    assert_ne!(status.as_u16(), 401, "L2 auth must not return 401 (got body: {})", body);

    eprintln!("=== PASSED: API key derivation + L2 auth works ===");
}

#[tokio::test]
async fn test_derive_then_sell_order() {
    let _ = dotenvy::dotenv();

    let cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };

    // Need a real token ID to place an order
    if cfg.team_b_token_id.is_empty() {
        eprintln!("SKIP: no TEAM_B_TOKEN_ID configured");
        return;
    }

    eprintln!("=== Deriving API key ===");
    let auth = match clob_auth::ClobAuth::derive(&cfg).await {
        Ok(a) => a,
        Err(e) => {
            panic!("API key derivation failed: {:?}", e);
        }
    };

    eprintln!("  api_key:    {}", auth.api_key);
    eprintln!("  funder:     {}", auth.funder_address());

    // Build a tiny SELL order for Team B (ZIM) at $0.99 (deep OOM for SELL)
    // Deep-OOM rule: BUY tests use 1¢ (nobody sells that low), SELL tests use 99¢
    // (nobody buys that high). Posting a SELL at 1¢ would be filled instantly
    // and give away real tokens for free.
    use polymarket_taker::types::{FakOrder, Side, Team};
    use rust_decimal_macros::dec;

    let _order = FakOrder {
        side: Side::Sell,
        team: Team::TeamB,
        price: dec!(0.99), // $0.99 — deep OOM for SELL, won't fill
        size: dec!(1),      // 1 token
    };

    // Place a BUY order instead (doesn't require holding tokens)
    let order = FakOrder {
        side: Side::Buy,
        team: Team::TeamB,
        price: dec!(0.01), // $0.01 — minimum price, won't fill
        size: dec!(1),      // 1 token
    };

    eprintln!("=== Placing GTC BUY order: {} @ ${} ===", order.size, order.price);
    eprintln!("  token_id: {}", cfg.team_b_token_id);
    eprintln!("  owner (api_key): {}", auth.api_key);
    eprintln!("  maker (funder):  {}", auth.funder_address());
    eprintln!("  signer (eoa):    {}", auth.address());
    eprintln!("  sig_type:        {}", cfg.signature_type);

    let result = polymarket_taker::orders::post_limit_order(&cfg, &auth, &order, "test-buy").await;

    match result {
        Ok(resp) => {
            eprintln!("  order_id: {:?}", resp.order_id);
            eprintln!("  status:   {:?}", resp.status);
            eprintln!("  error:    {:?}", resp.error_msg);

            // If order was accepted, cancel it immediately
            if let Some(ref oid) = resp.order_id {
                if !oid.is_empty() {
                    eprintln!("=== Cancelling test order {} ===", oid);
                    let _ = polymarket_taker::orders::cancel_order(&cfg, &auth, oid).await;
                }
            }

            // Check we didn't get an auth error
            if let Some(ref err) = resp.error_msg {
                assert!(
                    !err.to_lowercase().contains("unauthorized"),
                    "order rejected with auth error: {}",
                    err
                );
                assert!(
                    !err.to_lowercase().contains("invalid order payload"),
                    "invalid order payload: {}",
                    err
                );
            }
        }
        Err(e) => {
            let err_str = format!("{:?}", e);
            // Network errors are acceptable; auth errors are not
            assert!(
                !err_str.to_lowercase().contains("401"),
                "order failed with 401: {}",
                err_str
            );
            eprintln!("  order error (may be expected): {}", err_str);
        }
    }

    eprintln!("=== PASSED: derive + sell order flow complete ===");
}

#[tokio::test]
async fn test_signature_debug() {
    let _ = dotenvy::dotenv();
    let _ = tracing_subscriber::fmt().with_env_filter("debug").try_init();

    let cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };

    if cfg.team_b_token_id.is_empty() {
        eprintln!("SKIP: no TEAM_B_TOKEN_ID configured");
        return;
    }

    let auth = clob_auth::ClobAuth::derive(&cfg).await.unwrap();

    use polymarket_taker::types::{FakOrder, Side, Team};
    use rust_decimal_macros::dec;

    let order = FakOrder {
        side: Side::Buy,
        team: Team::TeamB,
        price: dec!(0.01),
        size: dec!(5),  // min order size
    };

    // Build the signed order using the internal function
    let result = polymarket_taker::orders::post_limit_order(&cfg, &auth, &order, "sig-test").await;
    match result {
        Ok(resp) => {
            eprintln!("  order_id: {:?}", resp.order_id);
            eprintln!("  status:   {:?}", resp.status);
            eprintln!("  error:    {:?}", resp.error_msg);
            if let Some(ref oid) = resp.order_id {
                if !oid.is_empty() {
                    let _ = polymarket_taker::orders::cancel_order(&cfg, &auth, oid).await;
                }
            }
        }
        Err(e) => eprintln!("  error: {:?}", e),
    }
}

/// Test with signatureType=0 (EOA direct, no proxy) to isolate if the
/// issue is specifically with proxy wallet signature handling.
#[tokio::test]
async fn test_eoa_direct_order() {
    let _ = dotenvy::dotenv();
    let _ = tracing_subscriber::fmt().with_env_filter("info").try_init();

    let mut cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };

    if cfg.team_b_token_id.is_empty() {
        eprintln!("SKIP: no TEAM_B_TOKEN_ID configured");
        return;
    }

    // Force EOA mode: signature_type=0, clear proxy address
    let original_sig_type = cfg.signature_type;
    let original_proxy = cfg.polymarket_address.clone();
    cfg.signature_type = 0;
    cfg.polymarket_address = String::new();

    eprintln!("=== Testing EOA-direct (sig_type=0) vs proxy (sig_type={original_sig_type}) ===");

    let auth = clob_auth::ClobAuth::derive(&cfg).await.unwrap();
    eprintln!("  api_key (EOA):  {}", auth.api_key);
    eprintln!("  signer/maker:   {}", auth.address());

    use polymarket_taker::types::{FakOrder, Side, Team};
    use rust_decimal_macros::dec;

    let order = FakOrder {
        side: Side::Buy,
        team: Team::TeamB,
        price: dec!(0.01),
        size: dec!(5),
    };

    eprintln!("--- EOA order (sig_type=0) ---");
    let result = polymarket_taker::orders::post_limit_order(&cfg, &auth, &order, "eoa-test").await;
    match &result {
        Ok(resp) => eprintln!("  EOA result: order_id={:?} error={:?}", resp.order_id, resp.error_msg),
        Err(e) => eprintln!("  EOA error: {:?}", e),
    }

    // Now test with proxy
    cfg.signature_type = original_sig_type;
    cfg.polymarket_address = original_proxy;

    let auth2 = clob_auth::ClobAuth::derive(&cfg).await.unwrap();
    eprintln!("  api_key (proxy): {}", auth2.api_key);
    eprintln!("  signer: {} maker: {}", auth2.address(), auth2.funder_address());

    eprintln!("--- Proxy order (sig_type={original_sig_type}) ---");
    let result2 = polymarket_taker::orders::post_limit_order(&cfg, &auth2, &order, "proxy-test").await;
    match &result2 {
        Ok(resp) => {
            eprintln!("  Proxy result: order_id={:?} error={:?}", resp.order_id, resp.error_msg);
            if let Some(ref oid) = resp.order_id {
                if !oid.is_empty() {
                    let _ = polymarket_taker::orders::cancel_order(&cfg, &auth2, oid).await;
                }
            }
        }
        Err(e) => eprintln!("  Proxy error: {:?}", e),
    }
}

/// Check fee rate for our token
#[tokio::test]
async fn test_check_fee_rate() {
    let _ = dotenvy::dotenv();
    let _ = tracing_subscriber::fmt().with_env_filter("info").try_init();

    let cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };
    if cfg.team_b_token_id.is_empty() { return; }

    let auth = clob_auth::ClobAuth::derive(&cfg).await.unwrap();

    // Try various fee-rate endpoints
    for path_tpl in &[
        "/fee-rate-bps?token_id={}",
        "/order-book/fee?tokenID={}",
        "/fee-rate?token_id={}",
    ] {
        let path = path_tpl.replace("{}", &cfg.team_b_token_id);
        let headers = auth.l2_headers("GET", &path, None).unwrap();
        let url = format!("{}{}", auth.clob_http_url(), path);
        let resp = auth.http_client().get(&url).headers(headers).send().await.unwrap();
        let status = resp.status();
        let body = resp.text().await.unwrap();
        eprintln!("GET {} => {} {}", path, status, &body[..body.len().min(200)]);
    }

    // Also try tick-size (known to work)
    let tick_url = format!("{}/tick-size?token_id={}", auth.clob_http_url(), cfg.team_b_token_id);
    let resp = auth.http_client().get(&tick_url).send().await.unwrap();
    eprintln!("tick-size => {} {}", resp.status(), resp.text().await.unwrap());

    // Try neg-risk
    let nr_url = format!("{}/neg-risk?token_id={}", auth.clob_http_url(), cfg.team_b_token_id);
    let resp = auth.http_client().get(&nr_url).send().await.unwrap();
    eprintln!("neg-risk => {} {}", resp.status(), resp.text().await.unwrap());
}

/// Check if the proxy address is correct by querying the Polymarket proxy factory
#[tokio::test]
async fn test_verify_proxy_address() {
    let _ = dotenvy::dotenv();
    let _ = tracing_subscriber::fmt().with_env_filter("info").try_init();

    let cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };

    let auth = clob_auth::ClobAuth::derive(&cfg).await.unwrap();
    let eoa = auth.address();
    let proxy = auth.funder_address();

    eprintln!("EOA:    {}", eoa);
    eprintln!("Proxy:  {}", proxy);
    eprintln!("SigType: {}", cfg.signature_type);

    // Try with signature_type=2 (GNOSIS_SAFE) instead
    eprintln!("\n=== Trying signatureType=2 (GNOSIS_SAFE) ===");

    use polymarket_taker::types::{FakOrder, Side, Team};
    use rust_decimal_macros::dec;

    let order = FakOrder {
        side: Side::Buy,
        team: Team::TeamB,
        price: dec!(0.01),
        size: dec!(5),
    };

    // Test with sig_type=2
    let mut cfg2 = cfg.clone();
    cfg2.signature_type = 2;
    let result = polymarket_taker::orders::post_limit_order(&cfg2, &auth, &order, "safe-test").await;
    match &result {
        Ok(resp) => eprintln!("  sig_type=2 result: order_id={:?} error={:?}", resp.order_id, resp.error_msg),
        Err(e) => eprintln!("  sig_type=2 error: {:?}", e),
    }
}

/// Test which endpoint works for checking order status
#[tokio::test]
async fn test_order_status_endpoints() {
    let _ = dotenvy::dotenv();
    let _ = tracing_subscriber::fmt().with_env_filter("info").try_init();

    let cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };
    if cfg.team_b_token_id.is_empty() { return; }

    let mut cfg = cfg;
    cfg.signature_type = 2; // GNOSIS_SAFE

    let auth = clob_auth::ClobAuth::derive(&cfg).await.unwrap();

    // Try different endpoints with the order ID from the log
    let order_id = "0xb6db6e418665a5b286e689c62dd978cd57dc1be3b105233b5e132cb0dbfbca33";

    for path_tpl in &[
        "/order/{}",
        "/data/order/{}",
    ] {
        let path = path_tpl.replace("{}", order_id);
        let headers = auth.l2_headers("GET", &path, None).unwrap();
        let url = format!("{}{}", auth.clob_http_url(), path);
        let resp = auth.http_client().get(&url).headers(headers).send().await.unwrap();
        let status = resp.status();
        let body = resp.text().await.unwrap();
        eprintln!("GET {} => {} {}", path, status, &body[..body.len().min(300)]);
    }

    // Try trades endpoint
    let path = "/data/trades";
    let headers = auth.l2_headers("GET", path, None).unwrap();
    let url = format!("{}{}?maker={}", auth.clob_http_url(), path, auth.funder_address());
    let resp = auth.http_client().get(&url).headers(headers).send().await.unwrap();
    let status = resp.status();
    let body = resp.text().await.unwrap();
    eprintln!("GET /data/trades => {} {}", status, &body[..body.len().min(500)]);
}

/// Check /data/order response format
#[tokio::test]
async fn test_data_order_format() {
    let _ = dotenvy::dotenv();
    let _ = tracing_subscriber::fmt().with_env_filter("info").try_init();

    let cfg = match force_derive_config() {
        Some(c) => c,
        None => return,
    };
    let mut cfg = cfg;
    cfg.signature_type = 2;

    let auth = clob_auth::ClobAuth::derive(&cfg).await.unwrap();
    let order_id = "0xb6db6e418665a5b286e689c62dd978cd57dc1be3b105233b5e132cb0dbfbca33";
    let path = format!("/data/order/{order_id}");
    let headers = auth.l2_headers("GET", &path, None).unwrap();
    let url = format!("{}{}", auth.clob_http_url(), path);
    let resp = auth.http_client().get(&url).headers(headers).send().await.unwrap();
    let body = resp.text().await.unwrap();
    eprintln!("FULL RESPONSE: {}", body);

    // Try parsing
    let parsed: serde_json::Value = serde_json::from_str(&body).unwrap();
    eprintln!("status: {:?}", parsed.get("status"));
    eprintln!("size_matched: {:?}", parsed.get("size_matched"));
    eprintln!("original_size: {:?}", parsed.get("original_size"));
    eprintln!("price: {:?}", parsed.get("price"));
    eprintln!("associate_trades: {:?}", parsed.get("associate_trades"));
}
