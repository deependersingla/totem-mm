use std::sync::Arc;
use std::time::{Duration, Instant};

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use tokio::sync::{broadcast, mpsc, watch};

use rand::Rng;

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::latency::LatencyMetric;
use crate::orders::{self, ClobOrder};
use crate::position::Position;
use crate::state::{AppState, PendingRevert, TradeRecord};
use crate::types::{CricketSignal, FakOrder, FillEvent, OrderBook, Side, SignalDirection, Team};

fn random_tag(prefix: &str) -> String {
    let suffix: String = rand::thread_rng()
        .sample_iter(rand::distributions::Alphanumeric)
        .take(6)
        .map(char::from)
        .collect();
    format!("{}_{}", prefix, suffix)
}

// ── Stale-revert dispatch logic ───────────────────────────────────────────
//
// When a new trade-triggering signal arrives, we decide between three actions:
//
//   NORMAL  — no pending reverts, fire a fresh FAK + place revert (existing path).
//   WAIT    — pending reverts are opposing the new signal direction (e.g., a W
//             left "buy A / sell B" reverts and another W arrives asking to
//             "sell A / buy B"). Do nothing, let the existing reverts sit.
//   AUGMENT — pending reverts are aligned with the new signal direction (e.g., a
//             4 left "sell A / buy B" reverts and a W arrives also wanting
//             "sell A / buy B"). Cancel and re-post those reverts at the
//             current book ± edge — no new FAK, just recovery.
//
// Rule collapses to: same signal type -> WAIT, opposite signal type -> AUGMENT.
// See brainstorm discussion in feat/stale-revert-dispatch thread.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DispatchAction {
    Normal,
    Wait,
    Augment,
}

/// Direction a pending revert's order would push the market if it filled.
/// Buying batting (or selling bowling) favors batting; the inverses favor bowling.
pub(crate) fn revert_action_direction(revert: &PendingRevert, batting: Team) -> SignalDirection {
    match (revert.team == batting, revert.side) {
        (true, Side::Buy)   => SignalDirection::FavorBatting,
        (true, Side::Sell)  => SignalDirection::FavorBowling,
        (false, Side::Sell) => SignalDirection::FavorBatting,
        (false, Side::Buy)  => SignalDirection::FavorBowling,
    }
}

/// Decide whether to NORMAL-fire, WAIT, or AUGMENT given pending revert state
/// and the direction of the new signal.
pub(crate) fn decide_dispatch_action(
    app: &Arc<AppState>,
    batting: Team,
    new_direction: SignalDirection,
) -> DispatchAction {
    let reverts = app.pending_reverts.lock().unwrap();
    if reverts.is_empty() {
        return DispatchAction::Normal;
    }

    // If any pending revert's action aligns with the new signal direction, augment.
    // (All reverts from a single event share a direction, so "any" == "all" in practice.)
    let any_aligned = reverts
        .iter()
        .any(|r| revert_action_direction(r, batting) == new_direction);

    if any_aligned {
        DispatchAction::Augment
    } else {
        DispatchAction::Wait
    }
}

pub async fn run(
    config: &Config,
    auth: &ClobAuth,
    mut signal_rx: broadcast::Receiver<CricketSignal>,
    book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    position: Position,
    app: Arc<AppState>,
    mut fill_rx: mpsc::Receiver<FillEvent>,
) {
    // Read current match state — innings 2 means India is already batting
    let mut state = app.match_state.read().unwrap().clone();

    tracing::info!(
        batting = %config.team_name(state.batting),
        bowling = %config.team_name(state.bowling()),
        innings = state.innings,
        dry_run = config.dry_run,
        "strategy engine started"
    );

    while let Ok(signal) = signal_rx.recv().await {
        let signal_time = Instant::now();
        let config = app.config.read().unwrap().clone();

        // Drain any pending fill events into a shared buffer for this cycle
        drain_fill_events(&mut fill_rx, &app);

        // ── Stale-revert dispatch ──────────────────────────────────────────
        // For trade-triggering signals, decide whether to fire a fresh trade
        // (NORMAL), skip because pending reverts fight the new direction
        // (WAIT), or re-price the pending reverts at current book (AUGMENT).
        // See helpers near top of file for the rule definitions.
        let new_direction = signal.trade_direction();
        let (event_seq, signal_tag) = if new_direction.is_some() {
            (app.next_event_seq(), signal.short_tag())
        } else {
            (0u64, String::new())
        };

        if let Some(dir) = new_direction {
            match decide_dispatch_action(&app, state.batting, dir) {
                DispatchAction::Wait => {
                    let n = app.pending_revert_count();
                    let msg = format!(
                        "event{event_seq} [{signal_tag}]: WAIT — {n} pending revert(s) oppose new direction"
                    );
                    tracing::info!("{msg}");
                    app.push_event("dispatch", &msg);
                    continue;
                }
                DispatchAction::Augment => {
                    let n = app.pending_revert_count();
                    let msg = format!(
                        "event{event_seq} [{signal_tag}]: AUGMENT — re-pricing {n} pending revert(s)"
                    );
                    tracing::info!("{msg}");
                    app.push_event("dispatch", &msg);
                    perform_augment(
                        &config,
                        auth,
                        &app,
                        &position,
                        state.batting,
                        &book_rx,
                        event_seq,
                        &signal_tag,
                    )
                    .await;
                    continue;
                }
                DispatchAction::Normal => {
                    // Fall through to the normal match signal path below.
                    tracing::debug!(
                        event_seq,
                        signal_tag = %signal_tag,
                        "dispatch: NORMAL (no pending reverts)"
                    );
                }
            }
        }

        match signal {
            CricketSignal::MatchOver => {
                tracing::info!("MO received — shutting down strategy");
                let pos = position.lock().unwrap();
                tracing::info!(position = %pos.summary(&config), "final position");
                app.push_event("strategy", "match over — strategy stopped");
                break;
            }

            CricketSignal::InningsOver => {
                let finished_innings = state.innings;
                state.switch_innings();
                *app.match_state.write().unwrap() = state.clone();
                if finished_innings >= 2 {
                    // T20: only 2 innings — don't advertise innings 3
                    let msg = format!("innings {} over — match complete", finished_innings);
                    tracing::info!("{msg}");
                    app.push_event("innings", &msg);
                } else {
                    let msg = format!("innings {} over — {} now batting (innings {})",
                        finished_innings, config.team_name(state.batting), state.innings);
                    tracing::info!("{msg}");
                    app.push_event("innings", &msg);
                }
            }

            CricketSignal::Wicket(extra_runs) => {
                let batting = state.batting;
                let bowling = state.bowling();
                if extra_runs > 0 {
                    app.push_event("ball", &format!("{extra_runs} runs on wicket ball"));
                }

                let msg = format!("WICKET — sell {} buy {}", config.team_name(batting), config.team_name(bowling));
                tracing::info!("{msg}");
                app.push_event("wicket", &msg);

                let books = book_rx.borrow().clone();

                if !price_in_safe_range(&config, &books) {
                    let (min, max) = config.safe_price_range();
                    let msg = format!("price outside {min}-{max} safe range — skipping trade");
                    tracing::info!("{msg}");
                    app.push_event("skip", &msg);
                    continue;
                }

                // Record signal-to-decision latency
                app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());

                let (batting_book, bowling_book) = team_books(&books, batting);

                // Try pre-signed cache first, fall back to fresh build
                let sell_order = if app.is_team_enabled(batting) {
                    let held = position.lock().unwrap().token_balance(batting);
                    resolve_sell_order(&config, &app, auth, batting, &batting_book, held, signal_time).await
                } else {
                    tracing::debug!(team = %config.team_name(batting), "team disabled — sell skipped");
                    None
                };
                let buy_order = if app.is_team_enabled(bowling) {
                    resolve_buy_order(&config, &app, auth, bowling, &bowling_book, signal_time).await
                } else {
                    tracing::debug!(team = %config.team_name(bowling), "team disabled — buy skipped");
                    None
                };

                if sell_order.is_none() {
                    let msg = format!("no bid on {} — sell leg skipped", config.team_name(batting));
                    tracing::warn!("{msg}");
                    app.push_event("warn", &msg);
                }
                if buy_order.is_none() {
                    let msg = format!("no ask on {} — buy leg skipped", config.team_name(bowling));
                    tracing::warn!("{msg}");
                    app.push_event("warn", &msg);
                }
                if sell_order.is_none() && buy_order.is_none() {
                    app.push_event("skip", "no book liquidity on either side — wicket trade skipped");
                    continue;
                }

                let task_config = config.clone();
                let task_auth = auth.clone();
                let task_position = position.clone();
                let task_app = app.clone();
                let task_book_rx = book_rx.clone();
                let task_signal_tag = signal_tag.clone();

                tokio::spawn(async move {
                    execute_event_trade(
                        &task_config, &task_auth, &task_position, &task_app,
                        task_book_rx, sell_order, buy_order, "WICKET",
                        event_seq, task_signal_tag, signal_time,
                    ).await;
                });
            }

            CricketSignal::Runs(r) => {
                tracing::debug!(runs = r, batting = %config.team_name(state.batting), "runs scored");
                app.push_event("ball", &format!("{r} runs"));
                if is_boundary(r) {
                    let books = book_rx.borrow().clone();
                    if price_in_safe_range(&config, &books) {
                        app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "RUN", event_seq, signal_tag.clone(), signal_time).await;
                    } else {
                        app.push_event("skip", &format!("RUN{r}: price outside safe range — boundary skipped"));
                    }
                }
            }
            CricketSignal::Wide(r) => {
                tracing::debug!(extra_runs = r, "wide");
                app.push_event("ball", &format!("Wd+{r}"));
                if is_boundary(r) {
                    let books = book_rx.borrow().clone();
                    if price_in_safe_range(&config, &books) {
                        app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "WD", event_seq, signal_tag.clone(), signal_time).await;
                    } else {
                        app.push_event("skip", &format!("WD{r}: price outside safe range — boundary skipped"));
                    }
                }
            }
            CricketSignal::NoBall(r) => {
                tracing::debug!(extra_runs = r, "no ball");
                app.push_event("ball", &format!("N+{r}"));
                if is_boundary(r) {
                    let books = book_rx.borrow().clone();
                    if price_in_safe_range(&config, &books) {
                        app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "NB", event_seq, signal_tag.clone(), signal_time).await;
                    } else {
                        app.push_event("skip", &format!("NB{r}: price outside safe range — boundary skipped"));
                    }
                }
            }
        }
    }

    tracing::info!("strategy engine stopped");
}

/// Drain any pending fill events from the user WS channel into a shared buffer.
/// This keeps the channel from backing up and lets us quickly check for fills later.
fn drain_fill_events(fill_rx: &mut mpsc::Receiver<FillEvent>, app: &Arc<AppState>) {
    while let Ok(event) = fill_rx.try_recv() {
        tracing::debug!(
            order_id = %event.order_id, status = %event.status,
            filled = %event.filled_size, price = %event.avg_price,
            "[FILL-DRAIN] buffered WS fill event"
        );
        app.buffer_fill_event(event);
    }
}

// ── Pre-signed order resolution ───────────────────────────────────────────

/// Resolved order: either from pre-signed cache or freshly built.
#[derive(Debug, Clone)]
pub(crate) struct ResolvedOrder {
    pub fak: FakOrder,
    pub signed: Option<ClobOrder>, // Some = pre-signed, None = needs signing
}

/// Try to get a pre-signed sell from cache. Fall back to fresh build.
async fn resolve_sell_order(
    config: &Config,
    app: &Arc<AppState>,
    _auth: &ClobAuth,
    team: Team,
    book: &OrderBook,
    held: Decimal,
    _signal_time: Instant,
) -> Option<ResolvedOrder> {
    // Try cache first
    if let Some(cached) = app.order_cache.take(team, Side::Sell).await {
        // Validate cache is still usable: price must match current best bid
        // AND size must not exceed currently held tokens (position may have changed since signing)
        if let Some(best_bid) = book.best_bid() {
            let held_floor = held.floor();
            let size_ok = cached.fak.size <= held_floor && held_floor >= config.order_min_size;
            if cached.book_price == best_bid.price && size_ok {
                tracing::debug!(team = %config.team_name(team), price = %cached.book_price, size = %cached.fak.size, held = %held_floor, "using pre-signed SELL from cache");
                return Some(ResolvedOrder { fak: cached.fak, signed: Some(cached.signed) });
            }
            tracing::debug!(team = %config.team_name(team),
                cached_price = %cached.book_price, current_price = %best_bid.price,
                cached_size = %cached.fak.size, held = %held_floor,
                "cache stale or size exceeded — rebuilding SELL");
        }
    }

    // Fresh build
    let fak = build_sell_order(config, team, book, Some(held))?;
    Some(ResolvedOrder { fak, signed: None })
}

/// Try to get a pre-signed buy from cache. Fall back to fresh build.
async fn resolve_buy_order(
    config: &Config,
    app: &Arc<AppState>,
    _auth: &ClobAuth,
    team: Team,
    book: &OrderBook,
    _signal_time: Instant,
) -> Option<ResolvedOrder> {
    if let Some(cached) = app.order_cache.take(team, Side::Buy).await {
        if let Some(best_ask) = book.best_ask() {
            if cached.book_price == best_ask.price {
                tracing::debug!(team = %config.team_name(team), price = %cached.book_price, "using pre-signed BUY from cache");
                return Some(ResolvedOrder { fak: cached.fak, signed: Some(cached.signed) });
            }
            tracing::debug!(team = %config.team_name(team),
                cached = %cached.book_price, current = %best_ask.price,
                "cache stale — rebuilding BUY");
        }
    }

    let fak = build_buy_order(config, team, book)?;
    Some(ResolvedOrder { fak, signed: None })
}

/// Result of firing a single FAK order
struct FakResult {
    order_id: Option<String>,
    intended_order: FakOrder,
    tag: String,
}

/// Result after polling for fill
struct FillInfo {
    filled_size: Decimal,
    avg_price: Decimal,
    order: FakOrder,
    #[allow(dead_code)]
    tag: String,
    order_id: String,
}

/// Generic trade executor: fire sell+buy FAK pair, detect fills via WS+REST race,
/// record position, then place revert.
async fn execute_event_trade(
    config: &Config,
    auth: &ClobAuth,
    position: &Position,
    app: &Arc<AppState>,
    _book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    sell_order: Option<ResolvedOrder>,
    buy_order: Option<ResolvedOrder>,
    label: &str,
    event_seq: u64,
    signal_tag: String,
    signal_time: Instant,
) {
    let trade_start = tokio::time::Instant::now();

    let sell_desc = sell_order.as_ref()
        .map(|o| format!("SELL {} @ {} sz={}", config.team_name(o.fak.team), o.fak.price, o.fak.size))
        .unwrap_or_else(|| "no order".into());
    let buy_desc = buy_order.as_ref()
        .map(|o| format!("BUY {} @ {} sz={}", config.team_name(o.fak.team), o.fak.price, o.fak.size))
        .unwrap_or_else(|| "no order".into());

    let sell_tag = random_tag("sell");
    let buy_tag  = random_tag("buy");

    let (sell_result, buy_result) = fire_fak_batch(
        config, auth, position, app,
        sell_order, &sell_tag,
        buy_order,  &buy_tag,
        signal_time,
    ).await;

    let poll_interval = Duration::from_millis(config.fill_poll_interval_ms.max(200));
    let poll_timeout  = Duration::from_millis(config.fill_poll_timeout_ms);

    // Detect fills: race user WS events against REST polling (no hardcoded 3.5s sleep)
    let (sell_fill, buy_fill) = tokio::join!(
        detect_fill(auth, app, sell_result, poll_interval, poll_timeout, config, signal_time),
        detect_fill(auth, app, buy_result,  poll_interval, poll_timeout, config, signal_time),
    );

    if let Some(ref f) = sell_fill {
        let cost = f.filled_size * f.avg_price;
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size });
        let msg = format!("{label}: SELL {} {} @ {} = ${} filled",
            f.filled_size, config.team_name(f.order.team), f.avg_price, cost.round_dp(2));
        tracing::info!("{msg}");
        app.push_event("filled", &msg);
        app.log_trade(TradeRecord {
            ts: crate::state::ist_now(),
            side: "SELL".into(),
            team: config.team_name(f.order.team).to_string(),
            size: f.filled_size,
            price: f.avg_price,
            cost,
            order_type: "FAK".into(),
            label: label.to_string(),
            order_id: f.order_id.clone(),
        });
    }
    if let Some(ref f) = buy_fill {
        let cost = f.filled_size * f.avg_price;
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size });
        let msg = format!("{label}: BUY {} {} @ {} = ${} filled",
            f.filled_size, config.team_name(f.order.team), f.avg_price, cost.round_dp(2));
        tracing::info!("{msg}");
        app.push_event("filled", &msg);
        app.log_trade(TradeRecord {
            ts: crate::state::ist_now(),
            side: "BUY".into(),
            team: config.team_name(f.order.team).to_string(),
            size: f.filled_size,
            price: f.avg_price,
            cost,
            order_type: "FAK".into(),
            label: label.to_string(),
            order_id: f.order_id.clone(),
        });
    }
    app.snapshot_inventory();

    // Record e2e latency
    app.latency.record(LatencyMetric::E2eSignalToFill, signal_time.elapsed());

    if sell_fill.is_none() && buy_fill.is_none() {
        let msg = format!("{label}: no fills — sell=[{sell_desc}] buy=[{buy_desc}]");
        tracing::info!("{msg}");
        app.push_event("warn", &msg);
        return;
    }

    let elapsed = trade_start.elapsed();
    let revert_delay_dur = Duration::from_millis(config.revert_delay_ms);
    if elapsed < revert_delay_dur {
        tokio::time::sleep(revert_delay_dur - elapsed).await;
    }

    // Re-read config for latest settings
    let config = app.config.read().unwrap().clone();

    // Use cached tick_size from background task (no HTTP in hot path)
    let tick: Decimal = app.cached_tick_size.read().unwrap()
        .unwrap_or_else(|| config.tick_size.parse().unwrap_or(dec!(0.01)));

    let edge_ticks = edge_ticks_for_label(label, &config);
    let edge_amount = Decimal::from_f64_retain(edge_ticks).unwrap_or(Decimal::ZERO) * tick;

    tracing::info!(delay_ms = config.revert_delay_ms, edge_ticks, tick = %tick, edge_amount = %edge_amount, "{label} REVERT");
    app.push_event("revert", &format!("{label}: revert after {}ms (edge {edge_ticks} ticks = {edge_amount})", config.revert_delay_ms));

    // Determine tick precision for rounding (e.g., 0.01 → 2dp, 0.001 → 3dp)
    let tick_dp = tick.to_string()
        .split('.')
        .nth(1)
        .map_or(0, |frac| frac.trim_end_matches('0').len()) as u32;

    // Revert buy → sell back at avg_price + edge_ticks
    if let Some(f) = buy_fill {
        let limit_price = round_to_tick(f.avg_price + edge_amount, tick, tick_dp);
        let size = f.filled_size.round_dp(2);
        tracing::info!(
            team = %config.team_name(f.order.team),
            original = %f.avg_price, edge_ticks,
            limit_price = %limit_price, size = %size,
            "REVERT_SELL: GTC sell limit (original + {edge_ticks} ticks)"
        );
        let revert_order = FakOrder {
            team: f.order.team,
            side: Side::Sell,
            price: limit_price,
            size,
        };
        if let Some(oid) = execute_limit(&config, auth, &revert_order, position, "REVERT_SELL", app).await {
            let revert_label = format!("e{event_seq}_{label}_REVERT_SELL");
            app.push_revert(PendingRevert {
                order_id: oid.clone(),
                team: f.order.team,
                side: Side::Sell,
                size,
                entry_price: f.avg_price,
                revert_limit_price: limit_price,
                placed_at: Instant::now(),
                label: revert_label.clone(),
                event_seq,
                signal_tag: signal_tag.clone(),
            });
            spawn_revert_fill_monitor(
                config.clone(), auth.clone(), app.clone(), position.clone(),
                oid, f.order.team, Side::Sell, size, limit_price, revert_label,
            );
        }
    }
    // Revert sell → buy back at avg_price - edge_ticks
    if let Some(f) = sell_fill {
        let limit_price = round_to_tick((f.avg_price - edge_amount).max(tick), tick, tick_dp);
        let size = f.filled_size.round_dp(2);
        tracing::info!(
            team = %config.team_name(f.order.team),
            original = %f.avg_price, edge_ticks,
            limit_price = %limit_price, size = %size,
            "REVERT_BUY: GTC buy limit (original - {edge_ticks} ticks)"
        );
        let revert_order = FakOrder {
            team: f.order.team,
            side: Side::Buy,
            price: limit_price,
            size,
        };
        if let Some(oid) = execute_limit(&config, auth, &revert_order, position, "REVERT_BUY", app).await {
            let revert_label = format!("e{event_seq}_{label}_REVERT_BUY");
            app.push_revert(PendingRevert {
                order_id: oid.clone(),
                team: f.order.team,
                side: Side::Buy,
                size,
                entry_price: f.avg_price,
                revert_limit_price: limit_price,
                placed_at: Instant::now(),
                label: revert_label.clone(),
                event_seq,
                signal_tag: signal_tag.clone(),
            });
            spawn_revert_fill_monitor(
                config.clone(), auth.clone(), app.clone(), position.clone(),
                oid, f.order.team, Side::Buy, size, limit_price, revert_label,
            );
        }
    }
}

/// Spawn a background task that polls a revert GTC order until it fills.
fn spawn_revert_fill_monitor(
    config: Config,
    auth: ClobAuth,
    app: Arc<AppState>,
    position: Position,
    order_id: String,
    team: Team,
    side: Side,
    _size: Decimal,
    limit_price: Decimal,
    label: String,
) {
    tokio::spawn(async move {
        let poll_interval = Duration::from_secs(5);
        let max_polls = 720; // 1 hour max (5s x 720)

        for _ in 0..max_polls {
            tokio::time::sleep(poll_interval).await;

            // Check if revert was removed (cancelled by opposite event or reset)
            let still_pending = app.pending_reverts.lock().unwrap()
                .iter().any(|r| r.order_id == order_id);
            if !still_pending {
                tracing::debug!(order_id = %order_id, label = %label, "revert monitor: removed externally");
                return;
            }

            // Check WS fill buffer first (fast path)
            if let Some(fill) = app.take_fill_event(&order_id) {
                let filled = fill.filled_size;
                let price = if fill.avg_price.is_zero() { limit_price } else { fill.avg_price };
                let cost = filled * price;
                {
                    let mut pos = position.lock().unwrap();
                    pos.on_fill(&FakOrder { team, side, price, size: filled });
                }
                let msg = format!("{label}: REVERT FILLED {} {} @ {} = ${}",
                    side, config.team_name(team), price, cost.round_dp(2));
                tracing::info!("{msg}");
                app.push_event("filled", &msg);
                app.log_trade(TradeRecord {
                    ts: crate::state::ist_now(),
                    side: format!("{side}"),
                    team: config.team_name(team).to_string(),
                    size: filled,
                    price,
                    cost,
                    order_type: "GTC".into(),
                    label: label.clone(),
                    order_id: order_id.clone(),
                });
                // Log round-trip PnL
                if let Some(revert) = app.remove_revert(&order_id) {
                    log_round_trip(&app, &config, &revert, price, filled, &order_id);
                }
                app.snapshot_inventory();
                return;
            }

            // Fall back to REST
            match orders::get_order(&auth, &order_id).await {
                Ok(open_order) => {
                    let filled = open_order.filled_size();
                    let fill_price = open_order.fill_price();
                    let status = open_order.status.as_deref().unwrap_or("unknown").to_lowercase();

                    if !filled.is_zero() || status == "matched" {
                        let price = if fill_price.is_zero() { limit_price } else { fill_price };
                        let cost = filled * price;
                        {
                            let mut pos = position.lock().unwrap();
                            pos.on_fill(&FakOrder { team, side, price, size: filled });
                        }
                        let msg = format!("{label}: REVERT FILLED {} {} @ {} = ${}",
                            side, config.team_name(team), price, cost.round_dp(2));
                        tracing::info!("{msg}");
                        app.push_event("filled", &msg);
                        app.log_trade(TradeRecord {
                            ts: crate::state::ist_now(),
                            side: format!("{side}"),
                            team: config.team_name(team).to_string(),
                            size: filled,
                            price,
                            cost,
                            order_type: "GTC".into(),
                            label: label.clone(),
                            order_id: order_id.clone(),
                        });
                        // Log round-trip PnL
                        if let Some(revert) = app.remove_revert(&order_id) {
                            log_round_trip(&app, &config, &revert, price, filled, &order_id);
                        }
                        app.snapshot_inventory();
                        return;
                    }

                    if open_order.is_terminal() && filled.is_zero() {
                        tracing::info!(order_id = %order_id, status, "revert order terminal with no fill");
                        app.push_event("warn", &format!("{label}: revert {status} (no fill)"));
                        app.remove_revert(&order_id);
                        return;
                    }
                }
                Err(_) => {} // not indexed yet, keep polling
            }
        }

        tracing::warn!(order_id = %order_id, label = %label, "revert fill monitor timed out after 1h");
        app.remove_revert(&order_id);
    });
}

/// AUGMENT path: cancel all pending reverts, capture any partial fills that
/// happened before cancel, and re-post fresh GTC reverts at the current book ±
/// edge with the remaining unfilled size. Fires no new FAK — the goal is purely
/// to recover the position from whatever the previous FAK already bought/sold.
async fn perform_augment(
    config: &Config,
    auth: &ClobAuth,
    app: &Arc<AppState>,
    position: &Position,
    batting: Team,
    book_rx: &watch::Receiver<(OrderBook, OrderBook)>,
    new_event_seq: u64,
    new_signal_tag: &str,
) {
    // Snapshot the stale reverts under a short-lived lock; we'll process
    // them outside the lock since subsequent work involves async HTTP calls.
    let stale: Vec<PendingRevert> = {
        let reverts = app.pending_reverts.lock().unwrap();
        reverts.clone()
    };

    if stale.is_empty() {
        return;
    }

    let order_ids: Vec<String> = stale.iter().map(|r| r.order_id.clone()).collect();

    // Cancel on CLOB first (dry_run skips HTTP; ids already virtual).
    if !config.dry_run {
        match orders::cancel_orders_batch(auth, &order_ids).await {
            Ok(_) => {
                tracing::info!(
                    event_seq = new_event_seq,
                    count = order_ids.len(),
                    "[AUGMENT] batch cancel ok"
                );
            }
            Err(e) => {
                tracing::warn!(
                    error = %e,
                    event_seq = new_event_seq,
                    "[AUGMENT] batch cancel failed — aborting augment, leaving reverts in place"
                );
                app.push_event("error", &format!("augment cancel failed: {e}"));
                return;
            }
        }
    }

    // Drain the stale reverts from state now that they're cancelled on CLOB.
    // Leave any entries added concurrently (shouldn't happen, but safe).
    for oid in &order_ids {
        let _ = app.remove_revert(oid);
    }

    // Fetch current book prices for placing the replacement reverts.
    let books = book_rx.borrow().clone();
    let (batting_book, bowling_book) = team_books(&books, batting);
    let tick: Decimal = app
        .cached_tick_size
        .read()
        .unwrap()
        .unwrap_or_else(|| config.tick_size.parse().unwrap_or(dec!(0.01)));
    let tick_dp = tick
        .to_string()
        .split('.')
        .nth(1)
        .map_or(0, |frac| frac.trim_end_matches('0').len()) as u32;

    let mut reposted = 0usize;
    let mut skipped_filled = 0usize;

    for stale_rev in stale {
        // Discover any fill that happened before cancel landed. Prefer the WS
        // fill buffer (authoritative avg_price); fall back to REST get_order.
        let (filled_size, avg_price) = if let Some(fill) = app.take_fill_event(&stale_rev.order_id) {
            let px = if fill.avg_price.is_zero() {
                stale_rev.revert_limit_price
            } else {
                fill.avg_price
            };
            (fill.filled_size, px)
        } else if !config.dry_run {
            match orders::get_order(auth, &stale_rev.order_id).await {
                Ok(o) => {
                    let fp = o.fill_price();
                    let px = if fp.is_zero() {
                        stale_rev.revert_limit_price
                    } else {
                        fp
                    };
                    (o.filled_size(), px)
                }
                Err(_) => (Decimal::ZERO, stale_rev.revert_limit_price),
            }
        } else {
            (Decimal::ZERO, stale_rev.revert_limit_price)
        };

        // Attribute any pre-cancel fill to position + trade log, matching the
        // work the revert monitor would have done.
        if !filled_size.is_zero() {
            let cost = filled_size * avg_price;
            {
                let mut pos = position.lock().unwrap();
                pos.on_fill(&FakOrder {
                    team: stale_rev.team,
                    side: stale_rev.side,
                    price: avg_price,
                    size: filled_size,
                });
            }
            let msg = format!(
                "{}: REVERT FILLED {} {} @ {} = ${} [augment-captured]",
                stale_rev.label,
                stale_rev.side,
                config.team_name(stale_rev.team),
                avg_price,
                cost.round_dp(2)
            );
            tracing::info!("{msg}");
            app.push_event("filled", &msg);
            app.log_trade(TradeRecord {
                ts: crate::state::ist_now(),
                side: format!("{}", stale_rev.side),
                team: config.team_name(stale_rev.team).to_string(),
                size: filled_size,
                price: avg_price,
                cost,
                order_type: "GTC".into(),
                label: stale_rev.label.clone(),
                order_id: stale_rev.order_id.clone(),
            });
            log_round_trip(app, config, &stale_rev, avg_price, filled_size, &stale_rev.order_id);
        }

        let remaining = (stale_rev.size - filled_size).max(Decimal::ZERO);
        if remaining.is_zero() {
            skipped_filled += 1;
            continue;
        }

        // Re-anchor the limit to the current book on the appropriate side.
        let book = if stale_rev.team == batting {
            &batting_book
        } else {
            &bowling_book
        };
        let edge_ticks = edge_ticks_for_label(&stale_rev.label, config);
        let edge_amount = Decimal::from_f64_retain(edge_ticks).unwrap_or(Decimal::ZERO) * tick;

        let new_price = match stale_rev.side {
            // SELL revert: place above best ask so we remain a maker.
            Side::Sell => {
                let anchor = book
                    .best_ask()
                    .map(|l| l.price)
                    .or_else(|| book.best_bid().map(|l| l.price))
                    .unwrap_or(stale_rev.revert_limit_price);
                round_to_tick(anchor + edge_amount, tick, tick_dp)
            }
            // BUY revert: place below best bid so we remain a maker.
            Side::Buy => {
                let anchor = book
                    .best_bid()
                    .map(|l| l.price)
                    .or_else(|| book.best_ask().map(|l| l.price))
                    .unwrap_or(stale_rev.revert_limit_price);
                round_to_tick((anchor - edge_amount).max(tick), tick, tick_dp)
            }
        };

        let augment_label = format!("{}_AUGMENT_e{}", stale_rev.label, new_event_seq);
        let repost_order = FakOrder {
            team: stale_rev.team,
            side: stale_rev.side,
            price: new_price,
            size: remaining,
        };

        tracing::info!(
            event_seq = new_event_seq,
            old_order_id = %stale_rev.order_id,
            team = %config.team_name(stale_rev.team),
            side = %stale_rev.side,
            old_price = %stale_rev.revert_limit_price,
            new_price = %new_price,
            size = %remaining,
            "[AUGMENT] reposting revert"
        );

        if let Some(new_oid) =
            execute_limit(config, auth, &repost_order, position, &augment_label, app).await
        {
            app.push_revert(PendingRevert {
                order_id: new_oid.clone(),
                team: stale_rev.team,
                side: stale_rev.side,
                size: remaining,
                entry_price: stale_rev.entry_price,
                revert_limit_price: new_price,
                placed_at: Instant::now(),
                label: augment_label.clone(),
                event_seq: new_event_seq,
                signal_tag: new_signal_tag.to_string(),
            });
            spawn_revert_fill_monitor(
                config.clone(),
                auth.clone(),
                app.clone(),
                position.clone(),
                new_oid,
                stale_rev.team,
                stale_rev.side,
                remaining,
                new_price,
                augment_label,
            );
            reposted += 1;
        }
    }

    app.push_event(
        "augment",
        &format!(
            "event{new_event_seq} [{new_signal_tag}]: reposted {reposted}, captured-filled {skipped_filled}"
        ),
    );
    app.snapshot_inventory();
}

/// Background tick_size refresh task. Polls Gamma API every `interval` and
/// updates `app.cached_tick_size`. Removes HTTP call from the revert hot path.
pub async fn tick_size_refresher(
    app: Arc<AppState>,
    cancel: tokio_util::sync::CancellationToken,
) {
    let interval = Duration::from_secs(30);
    tracing::info!("[TICK-REFRESH] background tick_size refresher started (every 30s)");

    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::info!("[TICK-REFRESH] cancelled, stopping");
                return;
            }
            _ = tokio::time::sleep(interval) => {
                let config = app.config.read().unwrap().clone();
                if config.market_slug.is_empty() {
                    continue;
                }

                let url = format!("https://gamma-api.polymarket.com/markets?slug={}", config.market_slug);
                match reqwest::get(&url).await {
                    Ok(resp) => {
                        if let Ok(markets) = resp.json::<Vec<serde_json::Value>>().await {
                            if let Some(market) = markets.first() {
                                if let Some(tick_f64) = market["orderPriceMinTickSize"].as_f64() {
                                    if let Some(tick) = Decimal::from_f64_retain(tick_f64) {
                                        *app.cached_tick_size.write().unwrap() = Some(tick);

                                        // Update config if changed
                                        let old_tick: Decimal = config.tick_size.parse().unwrap_or(dec!(0.01));
                                        if tick != old_tick {
                                            tracing::warn!(old = %old_tick, new = %tick, "tick size changed!");
                                            app.push_event("warn", &format!("tick size changed: {old_tick} -> {tick}"));
                                            let mut cfg = app.config.write().unwrap();
                                            cfg.tick_size = tick.to_string();
                                            cfg.persist();
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Err(e) => {
                        tracing::debug!(error = %e, "[TICK-REFRESH] fetch failed");
                    }
                }
            }
        }
    }
}

/// Log a completed round-trip (FAK entry + GTC revert exit) to the DB.
/// PnL = exit_proceeds - entry_cost for each leg.
fn log_round_trip(
    app: &Arc<AppState>,
    config: &Config,
    revert: &PendingRevert,
    exit_price: Decimal,
    exit_size: Decimal,
    exit_order_id: &str,
) {
    // Round-trip PnL calculation:
    // If entry was BUY (revert is SELL): pnl = (exit_price - entry_price) * size
    // If entry was SELL (revert is BUY): pnl = (entry_price - exit_price) * size
    let pnl = match revert.side {
        Side::Sell => (exit_price - revert.entry_price) * exit_size, // entry was BUY, exit is SELL
        Side::Buy => (revert.entry_price - exit_price) * exit_size,  // entry was SELL, exit is BUY
    };

    let entry_side = match revert.side {
        Side::Sell => "BUY",  // revert sell means entry was buy
        Side::Buy => "SELL",  // revert buy means entry was sell
    };

    tracing::info!(
        team = %config.team_name(revert.team),
        entry_side, entry_price = %revert.entry_price,
        exit_price = %exit_price, size = %exit_size,
        pnl = %pnl.round_dp(4),
        label = %revert.label,
        "ROUND-TRIP complete"
    );

    app.push_event("pnl", &format!("{}: {} {} entry={} exit={} sz={} PnL=${}",
        revert.label, entry_side, config.team_name(revert.team),
        revert.entry_price, exit_price, exit_size, pnl.round_dp(4)));

    if let Some(ref db) = *app.db.read().unwrap() {
        let slug = config.market_slug.clone();
        let now = crate::state::ist_now();
        db.insert_round_trip(
            &revert.placed_at.elapsed().as_secs().to_string(), // approximate entry time
            &now,
            &config.team_name(revert.team),
            entry_side,
            &revert.entry_price.to_string(),
            &exit_price.to_string(),
            &exit_size.to_string(),
            &pnl.round_dp(6).to_string(),
            &revert.label,
            "", // entry_order_id not stored in PendingRevert — could add later
            exit_order_id,
            &slug,
        );
    }
}

/// Background CLOB order sync — polls /data/orders every 5s and upserts into DB.
/// Reconciles pending_reverts against actual CLOB state.
pub async fn clob_order_sync(
    app: Arc<AppState>,
    cancel: tokio_util::sync::CancellationToken,
) {
    let interval = Duration::from_secs(5);
    tracing::info!("[ORDER-SYNC] background CLOB order sync started (every 5s)");

    // Initial delay to let auth settle
    tokio::time::sleep(Duration::from_secs(2)).await;

    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::info!("[ORDER-SYNC] cancelled, stopping");
                return;
            }
            _ = tokio::time::sleep(interval) => {
                let config = app.config.read().unwrap().clone();

                // Skip sync in dry_run — no real orders on CLOB
                if config.dry_run {
                    continue;
                }

                let auth = match app.auth.read().unwrap().clone() {
                    Some(a) => a,
                    None => continue,
                };

                if !config.has_tokens() {
                    continue;
                }

                // Fetch orders for both tokens
                let (res_a, res_b) = tokio::join!(
                    orders::get_user_orders(&auth, Some(&config.team_a_token_id)),
                    orders::get_user_orders(&auth, Some(&config.team_b_token_id)),
                );

                let mut all_orders: Vec<(serde_json::Value, &str)> = Vec::new();
                if let Ok(ords) = res_a {
                    for o in ords { all_orders.push((o, &config.team_a_name)); }
                }
                if let Ok(ords) = res_b {
                    for o in ords { all_orders.push((o, &config.team_b_name)); }
                }

                // Upsert into DB and update in-memory state
                if let Some(ref db) = *app.db.read().unwrap() {
                    for (o, team_name) in &all_orders {
                        let order_id = o.get("id").and_then(|v| v.as_str()).unwrap_or("");
                        if order_id.is_empty() { continue; }

                        let row = crate::db::ClobOrderRow {
                            order_id: order_id.to_string(),
                            asset_id: o.get("asset_id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                            side: o.get("side").and_then(|v| v.as_str()).unwrap_or("BUY").to_string(),
                            price: o.get("price").and_then(|v| v.as_str()).unwrap_or("0").to_string(),
                            original_size: o.get("original_size").and_then(|v| v.as_str()).unwrap_or("0").to_string(),
                            size_matched: o.get("size_matched").and_then(|v| v.as_str()).unwrap_or("0").to_string(),
                            status: o.get("status").and_then(|v| v.as_str()).unwrap_or("unknown").to_string(),
                            order_type: o.get("type").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                            created_at: o.get("created_at").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                            team: team_name.to_string(),
                        };
                        db.upsert_clob_order(&row, &config.market_slug);
                    }
                }

                // Reconcile pending_reverts: if CLOB says "matched" or "cancelled", update
                let reverts: Vec<String> = app.pending_reverts.lock().unwrap()
                    .iter().map(|r| r.order_id.clone()).collect();
                for oid in &reverts {
                    for (o, _) in &all_orders {
                        let id = o.get("id").and_then(|v| v.as_str()).unwrap_or("");
                        if id != oid.as_str() { continue; }
                        let status = o.get("status").and_then(|v| v.as_str()).unwrap_or("");
                        let size_matched = o.get("size_matched").and_then(|v| v.as_str())
                            .and_then(|s| s.parse::<Decimal>().ok())
                            .unwrap_or(Decimal::ZERO);

                        if (status == "matched" || status == "cancelled" || status == "expired") && !size_matched.is_zero() {
                            // Revert filled — buffer as fill event for the revert monitor to pick up
                            let price = o.get("price").and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<Decimal>().ok())
                                .unwrap_or(Decimal::ZERO);
                            let side_str = o.get("side").and_then(|v| v.as_str()).unwrap_or("BUY");
                            let side = if side_str == "SELL" { Side::Sell } else { Side::Buy };

                            app.buffer_fill_event(FillEvent {
                                order_id: oid.clone(),
                                filled_size: size_matched,
                                avg_price: price,
                                status: status.to_string(),
                                asset_id: o.get("asset_id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                                side,
                            });
                            tracing::debug!(order_id = %oid, status, filled = %size_matched, "[ORDER-SYNC] reconciled revert fill");
                        }
                    }
                }
            }
        }
    }
}

fn is_boundary(runs: u8) -> bool {
    runs > 3
}

/// Spawn a boundary trade: sell bowling team, buy batting team, then revert.
async fn spawn_boundary_trade(
    config: Config,
    auth: ClobAuth,
    position: Position,
    app: Arc<AppState>,
    book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    books: (OrderBook, OrderBook),
    batting: Team,
    runs: u8,
    kind: &'static str,
    event_seq: u64,
    signal_tag: String,
    signal_time: Instant,
) {
    let bowling = batting.opponent();
    let (batting_book, bowling_book) = team_books(&books, batting);

    // Boundary: bowler got hit -> sell bowling (price drops), buy batting (price rises)
    let sell_order = if app.is_team_enabled(bowling) {
        let held = position.lock().unwrap().token_balance(bowling);
        resolve_sell_order(&config, &app, &auth, bowling, &bowling_book, held, signal_time).await
    } else {
        None
    };
    let buy_order = if app.is_team_enabled(batting) {
        resolve_buy_order(&config, &app, &auth, batting, &batting_book, signal_time).await
    } else {
        None
    };

    if sell_order.is_none() && buy_order.is_none() {
        app.push_event("skip", &format!("{kind}{runs}: no liquidity for boundary trade"));
        return;
    }

    let msg = format!("{kind}{runs} BOUNDARY — sell {} buy {}",
        config.team_name(bowling), config.team_name(batting));
    tracing::info!("{msg}");
    app.push_event("boundary", &msg);

    let label = format!("{kind}{runs}");
    tokio::spawn(async move {
        execute_event_trade(&config, &auth, &position, &app, book_rx, sell_order, buy_order, &label, event_seq, signal_tag, signal_time).await;
    });
}

/// Send sell + buy FAK orders together via POST /orders (batch endpoint).
/// Uses pre-signed orders from cache when available, falls back to fresh signing.
async fn fire_fak_batch(
    config: &Config,
    auth: &ClobAuth,
    position: &Position,
    app: &Arc<AppState>,
    sell_order: Option<ResolvedOrder>,
    sell_tag: &str,
    buy_order: Option<ResolvedOrder>,
    buy_tag: &str,
    _signal_time: Instant,
) -> (Option<FakResult>, Option<FakResult>) {
    // Budget check for buy order
    let buy_order = if let Some(ref buy) = buy_order {
        let notional = buy.fak.price * buy.fak.size;
        let can_spend = position.lock().unwrap().can_spend(notional);
        if !can_spend {
            tracing::warn!(tag = buy_tag, notional = %notional, "budget exceeded — buy skipped");
            app.push_event("warn", &format!("{buy_tag}: budget exceeded, skipping"));
            None
        } else {
            buy_order
        }
    } else {
        buy_order
    };

    // Dry run
    if config.dry_run {
        let dry = |order: Option<ResolvedOrder>, tag: &str| -> Option<FakResult> {
            let o = order?;
            let notional = o.fak.price * o.fak.size;
            tracing::info!(tag, side = %o.fak.side, team = %config.team_name(o.fak.team),
                price = %o.fak.price, size = %o.fak.size, notional = %notional,
                presigned = o.signed.is_some(),
                "[DRY RUN] would place FAK order");
            app.push_event("trade", &format!("[DRY] {tag}: {} {} @ {} sz={}",
                o.fak.side, config.team_name(o.fak.team), o.fak.price, o.fak.size));
            Some(FakResult { order_id: Some("dry_run".to_string()), intended_order: o.fak, tag: tag.to_string() })
        };
        return (dry(sell_order, sell_tag), dry(buy_order, buy_tag));
    }

    // Build batch — use pre-signed where available, sign fresh otherwise
    let sign_start = Instant::now();
    let mut batch_signed: Vec<(ClobOrder, &str)> = Vec::new();
    let mut fak_meta: Vec<(Option<usize>, FakOrder, String)> = Vec::new(); // (batch_idx, fak, tag)

    if let Some(ref sell) = sell_order {
        let signed = match &sell.signed {
            Some(s) => s.clone(),
            None => match orders::build_signed_order(config, auth, &sell.fak) {
                Ok(s) => s,
                Err(e) => {
                    tracing::error!(tag = sell_tag, error = %e, "failed to sign sell order");
                    app.push_event("error", &format!("{sell_tag}: sign failed — {e}"));
                    return (None, buy_order.map(|_| FakResult { order_id: None, intended_order: sell.fak.clone(), tag: sell_tag.to_string() }).and(None));
                }
            }
        };
        let idx = batch_signed.len();
        batch_signed.push((signed, sell_tag));
        fak_meta.push((Some(idx), sell.fak.clone(), sell_tag.to_string()));
    }

    if let Some(ref buy) = buy_order {
        let signed = match &buy.signed {
            Some(s) => Some(s.clone()),
            None => match orders::build_signed_order(config, auth, &buy.fak) {
                Ok(s) => Some(s),
                Err(e) => {
                    tracing::error!(tag = buy_tag, error = %e, "failed to sign buy order");
                    app.push_event("error", &format!("{buy_tag}: sign failed — {e}"));
                    None
                }
            }
        };
        if let Some(signed) = signed {
            let idx = batch_signed.len();
            batch_signed.push((signed, buy_tag));
            fak_meta.push((Some(idx), buy.fak.clone(), buy_tag.to_string()));
        } else {
            // Sign failed — record as no-index so we skip it in results
            fak_meta.push((None, buy.fak.clone(), buy_tag.to_string()));
            if batch_signed.is_empty() {
                return (None, None);
            }
        }
    }

    app.latency.record(LatencyMetric::SignToPost, sign_start.elapsed());

    if batch_signed.is_empty() {
        return (None, None);
    }

    let post_start = Instant::now();
    let batch_results = match orders::post_presigned_fak_batch(config, auth, &batch_signed).await {
        Ok(r) => {
            app.latency.record(LatencyMetric::PostToResponse, post_start.elapsed());
            r
        }
        Err(e) => {
            app.latency.record(LatencyMetric::PostToResponse, post_start.elapsed());
            tracing::error!(error = %e, "batch FAK order failed");
            app.push_event("error", &format!("batch: {e}"));
            return (None, None);
        }
    };

    // Map results back to sell/buy
    let mut sell_result: Option<FakResult> = None;
    let mut buy_result: Option<FakResult> = None;

    for (batch_idx_opt, fak, tag) in &fak_meta {
        let batch_idx = match batch_idx_opt {
            Some(i) => *i,
            None => continue,
        };
        let r = match batch_results.get(batch_idx) {
            Some(r) => r,
            None => continue,
        };
        let err = r.error_msg.as_deref().unwrap_or("");
        if !r.success.unwrap_or(false) || !err.is_empty() {
            if !err.is_empty() {
                app.push_event("error", &format!("{tag}: rejected — {err}"));
            }
            continue;
        }
        let oid = match r.order_id.as_deref().filter(|s| !s.is_empty()) {
            Some(o) => o,
            None => continue,
        };
        let status = r.status.as_deref().unwrap_or("unknown");
        app.push_event("trade", &format!("{tag}: FAK {} {} @ {} sz={} ({}) [{}]",
            fak.side, config.team_name(fak.team), fak.price, fak.size, oid, status));

        let result = FakResult {
            order_id: Some(oid.to_string()),
            intended_order: fak.clone(),
            tag: tag.to_string(),
        };

        if tag.starts_with("sell") {
            sell_result = Some(result);
        } else {
            buy_result = Some(result);
        }
    }

    (sell_result, buy_result)
}

/// Detect fill via racing user WS events against REST polling.
/// No hardcoded 3.5s sleep — starts checking immediately.
async fn detect_fill(
    auth: &ClobAuth,
    app: &Arc<AppState>,
    fak_result: Option<FakResult>,
    poll_interval: Duration,
    poll_timeout: Duration,
    _config: &Config,
    signal_time: Instant,
) -> Option<FillInfo> {
    let result = fak_result?;
    let order_id = result.order_id.as_deref().filter(|s| !s.is_empty())?;
    let oid = order_id.to_string();

    if order_id == "dry_run" {
        return Some(FillInfo {
            filled_size: result.intended_order.size,
            avg_price: result.intended_order.price,
            order: result.intended_order,
            tag: result.tag,
            order_id: oid,
        });
    }

    let deadline = tokio::time::Instant::now() + poll_timeout;

    // No initial delay — check WS buffer immediately (sub-microsecond).
    // Sports markets have ~3s matching delay but WS events arrive as soon as
    // the matching engine confirms, so we start checking at t=0.

    loop {
        // 1) Check WS fill buffer first (fast path — sub-millisecond)
        if let Some(fill) = app.take_fill_event(&oid) {
            app.latency.record(LatencyMetric::FillDetectWs, signal_time.elapsed());
            tracing::info!(
                tag = %result.tag, order_id, status = %fill.status,
                filled = %fill.filled_size, price = %fill.avg_price,
                "fill detected via WS"
            );
            app.push_event("fill", &format!("{}: filled {} @ {} [WS:{}]",
                result.tag, fill.filled_size, fill.avg_price, fill.status));
            return Some(FillInfo {
                filled_size: fill.filled_size,
                avg_price: if fill.avg_price.is_zero() { result.intended_order.price } else { fill.avg_price },
                order: result.intended_order,
                tag: result.tag,
                order_id: oid,
            });
        }

        // 2) REST poll fallback
        match orders::get_order(auth, order_id).await {
            Ok(open_order) => {
                let filled = open_order.filled_size();
                let price = open_order.fill_price();
                let status = open_order.status.as_deref().unwrap_or("unknown");

                if status.eq_ignore_ascii_case("delayed") {
                    tracing::debug!(tag = %result.tag, order_id, "order in 'delayed' state (sports matching delay)");
                } else {
                    tracing::debug!(
                        tag = %result.tag, order_id, status,
                        filled = %filled, price = %price,
                        "poll fill status"
                    );
                }

                if !filled.is_zero() {
                    app.latency.record(LatencyMetric::FillDetectPoll, signal_time.elapsed());
                    app.push_event("fill", &format!("{}: filled {} @ {} [REST:{}]",
                        result.tag, filled, price, status));
                    return Some(FillInfo {
                        filled_size: filled,
                        avg_price: if price.is_zero() { result.intended_order.price } else { price },
                        order: result.intended_order,
                        tag: result.tag,
                        order_id: oid,
                    });
                }

                if status.eq_ignore_ascii_case("matched") {
                    app.latency.record(LatencyMetric::FillDetectPoll, signal_time.elapsed());
                    tracing::info!(tag = %result.tag, order_id, "status=matched, treating as filled at order price");
                    let sz = result.intended_order.size;
                    let px = result.intended_order.price;
                    app.push_event("fill", &format!("{}: filled {} @ {} [REST:matched]", result.tag, sz, px));
                    return Some(FillInfo {
                        filled_size: sz,
                        avg_price: px,
                        order: result.intended_order,
                        tag: result.tag,
                        order_id: oid,
                    });
                }

                if open_order.is_terminal() {
                    tracing::warn!(tag = %result.tag, order_id, status, "FAK order terminal — no fill");
                    app.push_event("warn", &format!("{}: NO FILL — status {} (order killed)", result.tag, status));
                    return None;
                }
            }
            Err(e) => {
                tracing::debug!(tag = %result.tag, order_id, error = %e, "poll: get_order failed (may not be indexed yet)");
            }
        }

        if tokio::time::Instant::now() >= deadline {
            break;
        }

        tokio::time::sleep(poll_interval).await;
    }

    tracing::warn!(tag = %result.tag, order_id, "poll timed out — no fill confirmation");
    app.push_event("warn", &format!("{}: poll timed out, no fill confirmed", result.tag));
    None
}

pub(crate) fn price_in_safe_range(config: &Config, books: &(OrderBook, OrderBook)) -> bool {
    let (min, max) = config.safe_price_range();
    let check = |book: &OrderBook| -> bool {
        if let Some(bid) = book.best_bid() {
            if bid.price < min || bid.price > max { return false; }
        }
        if let Some(ask) = book.best_ask() {
            if ask.price < min || ask.price > max { return false; }
        }
        true
    };
    check(&books.0) && check(&books.1)
}

fn team_books(books: &(OrderBook, OrderBook), team: Team) -> (OrderBook, OrderBook) {
    match team {
        Team::TeamA => (books.0.clone(), books.1.clone()),
        Team::TeamB => (books.1.clone(), books.0.clone()),
    }
}

/// Build a SELL FAK at L+1 price, sized from budget.
/// Makers will pull liquidity on events — book depth is irrelevant.
/// Size = max_trade_usdc / price, capped by held tokens.
pub(crate) fn build_sell_order(config: &Config, team: Team, book: &OrderBook, held_tokens: Option<Decimal>) -> Option<FakOrder> {
    let levels = &book.bids.levels;
    if levels.is_empty() { return None; }

    // Price at L+1 if available, else L0
    let price = levels.get(1).map_or(levels[0].price, |l| l.price);

    // Size purely from budget
    let mut size = (config.max_trade_usdc / price).floor();

    // Cap to held tokens
    if let Some(held) = held_tokens {
        let held_floor = held.floor();
        if held_floor < config.order_min_size {
            return None;
        }
        size = size.min(held_floor);
    }

    if size < config.order_min_size { return None; }

    Some(FakOrder { team, side: Side::Sell, price, size })
}

/// Build a BUY FAK at L+1 price, sized from budget.
pub(crate) fn build_buy_order(config: &Config, team: Team, book: &OrderBook) -> Option<FakOrder> {
    let levels = &book.asks.levels;
    if levels.is_empty() { return None; }

    // Price at L+1 if available, else L0
    let price = levels.get(1).map_or(levels[0].price, |l| l.price);

    // Size purely from budget
    let mut size = (config.max_trade_usdc / price).floor();

    if size < config.order_min_size {
        size = config.order_min_size;
    }

    Some(FakOrder { team, side: Side::Buy, price, size })
}

/// Select the revert edge percentage based on the signal label.
pub(crate) fn edge_ticks_for_label(label: &str, config: &Config) -> f64 {
    if label == "WICKET" {
        config.edge_wicket
    } else if label.contains('6') {
        config.edge_boundary_6
    } else {
        config.edge_boundary_4
    }
}

/// Round a price to the nearest tick boundary.
/// e.g., round_to_tick(0.513, 0.01, 2) → 0.51
///       round_to_tick(0.5137, 0.001, 3) → 0.514
fn round_to_tick(price: Decimal, tick: Decimal, tick_dp: u32) -> Decimal {
    if tick.is_zero() {
        return price.round_dp(2);
    }
    // Round to nearest tick: (price / tick).round() * tick
    let ticks = (price / tick).round_dp(0);
    (ticks * tick).round_dp(tick_dp)
}

#[cfg(test)]
pub(crate) fn compute_size(config: &Config, available: &Decimal, price: Decimal) -> Decimal {
    if price.is_zero() { return Decimal::ZERO; }
    let max_tokens = config.max_trade_usdc / price;
    max_tokens.min(*available).floor()
}

/// Place a GTC limit order and return the order_id if successful.
async fn execute_limit(
    config: &Config, auth: &ClobAuth, order: &FakOrder,
    _position: &Position, tag: &str, app: &Arc<AppState>,
) -> Option<String> {
    if config.dry_run {
        let notional = order.price * order.size;
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, notional = %notional,
            "[DRY RUN] would place GTC limit order");
        app.push_event("trade", &format!("[DRY] {tag}: GTC {} {} @ {} sz={}", order.side, config.team_name(order.team), order.price, order.size));
        return Some(format!("dry_run_{tag}"));
    }

    match orders::post_limit_order(config, auth, order, tag).await {
        Ok(resp) if resp.order_id.is_some() => {
            let oid = resp.order_id.unwrap();
            let cost = order.price * order.size;
            tracing::info!(tag, order_id = oid, "GTC limit order placed");
            app.track_order(oid.clone());
            app.push_event("trade", &format!("{tag}: GTC {} {} @ {} sz={} = ${} ({})",
                order.side, config.team_name(order.team), order.price, order.size, cost.round_dp(2), oid));
            app.log_trade(TradeRecord {
                ts: crate::state::ist_now(),
                side: format!("{}", order.side),
                team: config.team_name(order.team).to_string(),
                size: order.size,
                price: order.price,
                cost,
                order_type: "GTC".into(),
                label: tag.to_string(),
                order_id: oid.clone(),
            });
            Some(oid)
        }
        Ok(resp) => {
            let msg = resp.error_msg.unwrap_or_default();
            app.push_event("error", &format!("{tag}: GTC rejected — {msg}"));
            None
        }
        Err(e) => {
            tracing::error!(tag, error = %e, "GTC limit order failed");
            app.push_event("error", &format!("{tag}: {e}"));
            None
        }
    }
}
