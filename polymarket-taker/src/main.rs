#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

use anyhow::Result;
use polymarket_taker::{clob_auth, config, db, server, state};

#[tokio::main]
async fn main() -> Result<()> {
    let config = config::Config::from_env()?;

    // B1 (TODO.md): trader-facing logs run on IST; the subscriber's timer is
    // the single place we configure that, so every `tracing::info!`/`warn!`/
    // `error!` line is timestamped IST without per-call instrumentation.
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| config.log_level.parse().unwrap_or_default()),
        )
        .with_target(false)
        .with_timer(polymarket_taker::state::IstTimer)
        .init();

    let port = config.http_port;

    tracing::info!(
        team_a = %config.team_a_name,
        team_b = %config.team_b_name,
        dry_run = config.dry_run,
        port,
        "totem-taker starting"
    );

    let sig_type_name = match config.signature_type {
        0 => "EOA",
        1 => "POLY_PROXY",
        2 => "GNOSIS_SAFE",
        _ => "UNKNOWN",
    };
    tracing::info!(
        signature_type = config.signature_type,
        signature_type_name = sig_type_name,
        polymarket_address = %config.polymarket_address,
        neg_risk = config.neg_risk,
        "wallet config"
    );

    if config.signature_type > 0 && config.polymarket_address.is_empty() {
        tracing::error!(
            "signature_type={} ({}) requires polymarket_address (funder/proxy wallet) to be set! \
             Check your Polymarket profile at polymarket.com/settings for the displayed address.",
            config.signature_type, sig_type_name
        );
    }

    let app_state = state::AppState::new(config.clone());

    // Initialize SQLite database for trade/order persistence
    match db::Db::open() {
        Ok(database) => {
            *app_state.db.write().unwrap() = Some(std::sync::Arc::new(database));
            tracing::info!("SQLite database initialized (taker.db)");
        }
        Err(e) => {
            tracing::warn!(error = %e, "failed to open SQLite database — trades will not persist");
        }
    }

    // Generate CLOB API keys only when wallet is configured (private key set).
    // Uses EIP-712 sign + GET /auth/derive-api-key (or POST /auth/api-key) — same flow as tests.
    if app_state.config.read().unwrap().has_wallet() {
        let cfg = app_state.config.read().unwrap().clone();
        let generating_from_wallet = cfg.api_key.is_empty()
            || cfg.api_secret.is_empty()
            || cfg.api_passphrase.is_empty();
        if generating_from_wallet {
            tracing::info!("Generating CLOB API keys from wallet (EIP-712 + derive-api-key)");
        }
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

    // Start orderbook WS immediately if tokens are already configured from settings.json
    server::start_book_ws(&app_state);

    let router = server::build_router(app_state);

    let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{port}")).await?;
    tracing::info!("HTTP server listening on 0.0.0.0:{port}");
    tracing::info!("open http://localhost:{port} in your browser");

    axum::serve(listener, router).await?;

    Ok(())
}
