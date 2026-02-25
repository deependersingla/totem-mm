mod clob_auth;
mod config;
mod market_ws;
mod orders;
mod position;
mod server;
mod signal;
mod state;
mod strategy;
mod types;
mod web;

use anyhow::Result;

#[tokio::main]
async fn main() -> Result<()> {
    let config = config::Config::from_env()?;

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| config.log_level.parse().unwrap_or_default()),
        )
        .with_target(false)
        .init();

    let port = config.http_port;

    tracing::info!(
        team_a = %config.team_a_name,
        team_b = %config.team_b_name,
        dry_run = config.dry_run,
        port,
        "totem-taker starting"
    );

    let app_state = state::AppState::new(config);

    if app_state.config.read().unwrap().has_wallet() {
        let cfg = app_state.config.read().unwrap().clone();
        match clob_auth::ClobAuth::derive(&cfg).await {
            Ok(auth) => {
                tracing::info!("CLOB auth initialized on startup");
                *app_state.auth.write().unwrap() = Some(auth);
            }
            Err(e) => {
                tracing::warn!(error = %e, "could not derive CLOB auth on startup — configure via UI");
            }
        }
    }

    let router = server::build_router(app_state);

    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{port}")).await?;
    tracing::info!("HTTP server listening on 0.0.0.0:{port}");
    tracing::info!("open http://localhost:{port} in your browser");

    axum::serve(listener, router).await?;

    Ok(())
}
