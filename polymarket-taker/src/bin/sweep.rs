#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

use anyhow::Result;
use polymarket_taker::{clob_auth, state, sweep_config, sweep_server, sweep_state};

#[tokio::main]
async fn main() -> Result<()> {
    let config = sweep_config::SweepAppConfig::from_env()?;

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| config.log_level.parse().unwrap_or_default()),
        )
        .with_target(false)
        .with_timer(state::IstTimer)
        .init();

    let port = config.http_port;

    tracing::info!(
        team_a = %config.team_a_name,
        team_b = %config.team_b_name,
        dry_run = config.dry_run,
        port,
        "totem-sweep starting"
    );

    let state = sweep_state::SweepAppState::new(config.clone());

    // Derive CLOB auth if wallet is configured
    if config.has_wallet() {
        let shared = config.to_shared_config();
        match clob_auth::ClobAuth::derive(&shared).await {
            Ok(auth) => {
                tracing::info!("CLOB auth initialized");
                *state.auth.write().unwrap() = Some(auth);
            }
            Err(e) => {
                tracing::warn!(error = %e, "could not derive CLOB auth — configure via UI");
            }
        }
    }

    // Start orderbook WS if tokens are configured
    sweep_server::start_book_ws(&state);

    let router = sweep_server::build_router(state);

    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{port}")).await?;
    tracing::info!("sweep HTTP server on http://localhost:{port}");

    axum::serve(listener, router).await?;

    Ok(())
}
