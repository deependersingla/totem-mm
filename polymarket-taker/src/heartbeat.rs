//! Heartbeat — keeps GTC/GTD orders alive on Polymarket CLOB.
//!
//! Sends POST /heartbeats every 10 seconds with L2 auth headers.
//! Without heartbeat, all open orders are auto-cancelled by the exchange.

use std::sync::Arc;
use std::time::Duration;

use tokio::time::interval;
use tokio_util::sync::CancellationToken;

use crate::clob_auth::ClobAuth;
use crate::state::AppState;

pub(crate) const HEARTBEAT_PATH: &str = "/heartbeats";
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);

/// Send a single heartbeat using the given auth.
/// Standalone — no AppState dependency, usable by both taker and sweep.
pub async fn send_heartbeat_with_auth(auth: &ClobAuth) -> anyhow::Result<()> {
    let url = format!("{}{}", auth.clob_http_url(), HEARTBEAT_PATH);
    let headers = auth.l2_headers("POST", HEARTBEAT_PATH, None)?;

    let resp = auth
        .http_client()
        .post(&url)
        .headers(headers)
        .send()
        .await?;

    let status = resp.status();
    if status.is_success() {
        tracing::debug!("[HEARTBEAT] ok");
    } else {
        let body = resp.text().await.unwrap_or_default();
        tracing::warn!("[HEARTBEAT] HTTP {status}: {body}");
    }

    Ok(())
}

/// Heartbeat loop using AppState (taker/maker).
pub async fn run(state: Arc<AppState>, cancel: CancellationToken) {
    tracing::info!("[HEARTBEAT] starting (interval={:?})", HEARTBEAT_INTERVAL);

    let mut timer = interval(HEARTBEAT_INTERVAL);
    timer.tick().await;

    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::info!("[HEARTBEAT] cancelled, stopping");
                return;
            }
            _ = timer.tick() => {
                let auth = state.auth.read().unwrap().clone();
                if let Some(ref a) = auth {
                    if let Err(e) = send_heartbeat_with_auth(a).await {
                        tracing::warn!("[HEARTBEAT] failed: {e}");
                    }
                }
            }
        }
    }
}

/// Standalone heartbeat loop — takes ClobAuth directly, no AppState.
/// Used by the sweep binary.
pub async fn run_standalone(auth: ClobAuth, cancel: CancellationToken) {
    tracing::info!("[HEARTBEAT] starting standalone (interval={:?})", HEARTBEAT_INTERVAL);

    let mut timer = interval(HEARTBEAT_INTERVAL);
    timer.tick().await;

    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::info!("[HEARTBEAT] stopped");
                return;
            }
            _ = timer.tick() => {
                if let Err(e) = send_heartbeat_with_auth(&auth).await {
                    tracing::warn!("[HEARTBEAT] failed: {e}");
                }
            }
        }
    }
}
