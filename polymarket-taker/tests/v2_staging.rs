//! End-to-end V2 integration test against a live Polymarket endpoint.
//!
//! Default endpoint is `https://clob-v2.polymarket.com` (V2 staging). After
//! the 2026-04-28 ~11:00 UTC cutover, override with
//! `POLYMARKET_CLOB_HTTP=https://clob.polymarket.com` to validate against
//! production V2.
//!
//! Test path:
//!   1. Derive API key from POLYMARKET_PRIVATE_KEY (V1-style ClobAuth, unchanged)
//!   2. Build a tiny GTC BUY at $0.01 (deep OOM, won't fill)
//!   3. Sign with orders_v2 (V2 EIP-712)
//!   4. POST /order with HMAC L2 headers
//!   5. Cancel immediately if accepted
//!
//! Acceptance rule: request must NOT be rejected with a *signature* error
//! ("invalid signature", "signature mismatch", etc). A market-data error
//! ("orderbook does not exist", "min order size", etc.) is fine — it proves
//! the V2 signature was accepted by the server.
//!
//! Gated by `POLYMARKET_V2_STAGING=1` so it does not run on default
//! `cargo test`. To run:
//!   POLYMARKET_V2_STAGING=1 cargo test --test v2_staging -- --nocapture

use polymarket_taker::{clob_auth, config, orders, types::{FakOrder, Side, Team}};
use rust_decimal_macros::dec;

const SIGNATURE_REJECT_KEYWORDS: &[&str] = &[
    "invalid signature",
    "signature mismatch",
    "bad signature",
    "signature verification",
    "eip-712",
    "eip712",
    "domain mismatch",
    "type hash",
];

fn skip_unless_enabled() -> bool {
    if std::env::var("POLYMARKET_V2_STAGING").ok().as_deref() != Some("1") {
        eprintln!("SKIP: set POLYMARKET_V2_STAGING=1 to run this test");
        return true;
    }
    false
}

fn load_cfg() -> Option<config::Config> {
    let _ = dotenvy::dotenv();
    let cfg = config::Config::from_env().ok()?;
    if !cfg.has_wallet() {
        eprintln!("SKIP: no POLYMARKET_PRIVATE_KEY in env");
        return None;
    }
    if cfg.team_a_token_id.is_empty() && cfg.team_b_token_id.is_empty() {
        eprintln!("SKIP: no token IDs configured (TEAM_A_TOKEN_ID / TEAM_B_TOKEN_ID)");
        return None;
    }
    Some(cfg)
}

#[tokio::test]
async fn test_v2_signed_order_accepted_by_endpoint() {
    if skip_unless_enabled() { return; }
    let _ = tracing_subscriber::fmt().with_env_filter("info").try_init();

    let cfg = match load_cfg() { Some(c) => c, None => return };

    eprintln!("=== V2 staging test ===");
    eprintln!("  endpoint:     {}", cfg.clob_http);
    eprintln!("  exchange (V2): {}", cfg.exchange_address());
    eprintln!("  chain_id:     {}", cfg.chain_id);
    eprintln!("  builder_code: {}", if cfg.builder_code.is_empty() { "(none)" } else { "(set)" });

    let auth = clob_auth::ClobAuth::derive(&cfg).await.expect("API key derivation");
    eprintln!("  api_key:      {}", auth.api_key);
    eprintln!("  signer:       {}", auth.address());
    eprintln!("  funder:       {}", auth.funder_address());

    let team = if !cfg.team_b_token_id.is_empty() { Team::TeamB } else { Team::TeamA };
    let order = FakOrder {
        side: Side::Buy,
        team,
        price: dec!(0.01),  // deep out-of-money — won't fill at this price
        size: dec!(5),       // typical min order size
    };

    eprintln!("=== Posting V2-signed GTC BUY: {} @ ${} on {:?} ===", order.size, order.price, team);
    let resp = orders::post_limit_order(&cfg, &auth, &order, "v2-staging-test").await
        .expect("post_limit_order should not produce a transport error");

    eprintln!("  order_id: {:?}", resp.order_id);
    eprintln!("  status:   {:?}", resp.status);
    eprintln!("  error:    {:?}", resp.error_msg);

    // Cleanup: cancel if accepted.
    if let Some(oid) = resp.order_id.as_deref().filter(|s| !s.is_empty()) {
        let _ = orders::cancel_order(&cfg, &auth, oid).await;
        eprintln!("=== Cancelled test order {oid} ===");
    }

    // Acceptance: signature must not be the rejection cause.
    if let Some(err) = resp.error_msg.as_deref() {
        let lower = err.to_lowercase();
        for keyword in SIGNATURE_REJECT_KEYWORDS {
            assert!(
                !lower.contains(keyword),
                "V2 signature rejected by endpoint (keyword '{keyword}'): {err}"
            );
        }
        // Auth errors are also fatal — means our HMAC L2 headers are wrong.
        assert!(
            !lower.contains("unauthorized") && !lower.contains("401"),
            "V2 L2 auth rejected: {err}"
        );
        eprintln!("Note: order was rejected for a non-signature reason (acceptable): {err}");
    }

    eprintln!("=== PASSED: V2 signature accepted by {} ===", cfg.clob_http);
}
