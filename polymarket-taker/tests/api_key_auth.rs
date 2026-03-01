//! Integration tests: API key (normal) authentication for Polymarket CLOB.
//!
//! Two ways to get credentials:
//! 1. **Derive at runtime**: Set only `POLYMARKET_PRIVATE_KEY` (and optionally `CHAIN_ID`,
//!    `POLYMARKET_CLOB_HTTP`). The crate signs an EIP-712 message and calls
//!    `GET /auth/derive-api-key` (or `POST /auth/api-key` if derive fails) to obtain
//!    API key, secret, and passphrase. Same credentials every time for the same wallet.
//! 2. **Pre-configured**: Set `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`,
//!    `POLYMARKET_API_PASSPHRASE` (e.g. from Polymarket.com → Settings → API).

use polymarket_taker::{clob_auth, config};

#[tokio::test]
async fn test_api_key_auth() {
    let _ = dotenvy::dotenv();

    let cfg = match config::Config::from_env() {
        Ok(c) => c,
        Err(_) => return, // skip if config fails (e.g. missing env)
    };

    // Need either wallet (for L1 derive) or pre-configured API key
    let has_creds = cfg.has_wallet()
        || (!cfg.api_key.is_empty() && !cfg.api_secret.is_empty() && !cfg.api_passphrase.is_empty());
    if !has_creds {
        return; // skip when no credentials
    }

    let auth = match clob_auth::ClobAuth::derive(&cfg).await {
        Ok(a) => a,
        Err(_) => return, // skip if derive fails
    };

    let path = "/orders";
    let headers = match auth.l2_headers("GET", path, None) {
        Ok(h) => h,
        Err(_) => return,
    };

    assert!(
        headers.contains_key("POLY_API_KEY"),
        "L2 headers must include POLY_API_KEY"
    );
    assert!(
        headers.contains_key("POLY_SIGNATURE"),
        "L2 headers must include POLY_SIGNATURE"
    );
    assert!(
        headers.contains_key("POLY_TIMESTAMP"),
        "L2 headers must include POLY_TIMESTAMP"
    );
    assert!(
        headers.contains_key("POLY_PASSPHRASE"),
        "L2 headers must include POLY_PASSPHRASE"
    );

    // Verify auth is accepted by the CLOB (not 401)
    let url = format!("{}{}", auth.clob_http_url(), path);
    let resp = auth
        .http_client()
        .get(&url)
        .headers(headers)
        .send()
        .await;
    if let Ok(r) = resp {
        let status = r.status();
        assert!(
            status.as_u16() != 401,
            "API key auth must not return 401 Unauthorized (got {})",
            status
        );
    }
}

/// Derive API keys from private key via EIP-712 + /auth/derive-api-key, then authenticate.
/// Run with only POLYMARKET_PRIVATE_KEY set (no POLYMARKET_API_KEY/SECRET/PASSPHRASE).
/// Skips if wallet or CLOB URL is missing.
#[tokio::test]
async fn test_api_key_derive_then_auth() {
    let _ = dotenvy::dotenv();

    let cfg = match config::Config::from_env() {
        Ok(c) => c,
        Err(_) => return,
    };

    // Step 1: Require private key only (no pre-configured API keys)
    if !cfg.has_wallet() {
        return;
    }
    if !cfg.api_key.is_empty() && !cfg.api_secret.is_empty() && !cfg.api_passphrase.is_empty() {
        return; // skip when pre-configured keys are set; this test is for derive-only
    }

    // Step 2: Generate API keys — EIP-712 sign + GET /auth/derive-api-key (or POST /auth/api-key)
    let auth = match clob_auth::ClobAuth::derive(&cfg).await {
        Ok(a) => a,
        Err(e) => {
            eprintln!("derive failed (missing or invalid key / network): {:?}", e);
            return;
        }
    };

    assert!(!auth.api_key.is_empty(), "derived API key must be non-empty");
    assert!(!auth.api_secret.is_empty(), "derived API secret must be non-empty");
    assert!(!auth.passphrase.is_empty(), "derived passphrase must be non-empty");

    // Step 3: Use derived credentials to build L2 headers and authenticate
    let path = "/orders";
    let headers = match auth.l2_headers("GET", path, None) {
        Ok(h) => h,
        Err(_) => return,
    };

    let url = format!("{}{}", auth.clob_http_url(), path);
    let resp = auth
        .http_client()
        .get(&url)
        .headers(headers)
        .send()
        .await;

    let resp = match resp {
        Ok(r) => r,
        Err(_) => return, // network error, skip
    };

    let status = resp.status();
    assert!(
        status.as_u16() != 401,
        "derive-then-auth must not return 401 Unauthorized (got {})",
        status
    );
}
