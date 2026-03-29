//! Heartbeat — keeps GTC/GTD orders alive on Polymarket CLOB.
//!
//! Sends POST /heartbeats every 10 seconds with L2 auth headers.
//! Without heartbeat, all open orders are auto-cancelled by the exchange.

use std::sync::Arc;
use std::time::Duration;

use tokio::time::interval;
use tokio_util::sync::CancellationToken;

use crate::state::AppState;

pub(crate) const HEARTBEAT_PATH: &str = "/heartbeats";
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);

pub async fn run(state: Arc<AppState>, cancel: CancellationToken) {
    tracing::info!("[HEARTBEAT] starting (interval={:?})", HEARTBEAT_INTERVAL);

    let mut timer = interval(HEARTBEAT_INTERVAL);
    timer.tick().await; // consume first immediate tick

    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::info!("[HEARTBEAT] cancelled, stopping");
                return;
            }
            _ = timer.tick() => {
                if let Err(e) = send_heartbeat(&state).await {
                    tracing::warn!("[HEARTBEAT] failed: {e}");
                }
            }
        }
    }
}

async fn send_heartbeat(state: &AppState) -> anyhow::Result<()> {
    let auth = state.auth.read().unwrap().clone();
    let auth = match auth {
        Some(a) => a,
        None => {
            tracing::debug!("[HEARTBEAT] no auth configured, skipping");
            return Ok(());
        }
    };

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
