use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc;

use crate::types::CricketSignal;

/// Reads cricket signals from stdin, one per line.
/// For testing: type "W", "4", "IO", "MO" etc. into the terminal.
/// In production, this will be replaced by a telegram bot listener.
pub async fn run_stdin(signal_tx: mpsc::Sender<CricketSignal>) {
    tracing::info!("signal listener started (stdin mode)");
    tracing::info!("enter signals: 0-6, W, Wd, 1Wd, N, IO, MO");

    let stdin = tokio::io::stdin();
    let reader = BufReader::new(stdin);
    let mut lines = reader.lines();

    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                let raw = line.trim().to_string();
                if raw.is_empty() {
                    continue;
                }

                match CricketSignal::parse(&raw) {
                    Some(signal) => {
                        tracing::info!(signal = %signal, "signal received");
                        if signal == CricketSignal::MatchOver {
                            let _ = signal_tx.send(signal).await;
                            tracing::info!("match over — signal listener stopping");
                            return;
                        }
                        if signal_tx.send(signal).await.is_err() {
                            tracing::error!("signal channel closed");
                            return;
                        }
                    }
                    None => {
                        tracing::warn!(input = raw, "unknown signal, ignoring");
                    }
                }
            }
            Ok(None) => {
                tracing::info!("stdin closed — signal listener stopping");
                return;
            }
            Err(e) => {
                tracing::error!(error = %e, "stdin read error");
                return;
            }
        }
    }
}
