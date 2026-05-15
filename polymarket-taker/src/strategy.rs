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
use crate::state::{AppState, DispatchedSignal, PendingRevert, TradeRecord};
use crate::price_history::TouchSnapshot;
use crate::types::{CricketSignal, FakOrder, FillEvent, OrderBook, Side, SignalDirection, Team};
use crate::ws_health::{ws_blocks_trading_reason, WsHealth};

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
    mut signal_rx: broadcast::Receiver<DispatchedSignal>,
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

    while let Ok(dispatched) = signal_rx.recv().await {
        let signal_time = Instant::now();
        let config = app.config.read().unwrap().clone();

        // event_seq, correlation_id, and ts_ist are allocated by the HTTP
        // handler at receive time (see `AppState::make_dispatch`). The
        // handler also wrote the corresponding ledger row before broadcasting
        // — see `db.update_oracle_event_decision` below for the WAIT/AUGMENT/
        // NORMAL update path.
        let signal = dispatched.signal.clone();
        let event_seq = dispatched.event_seq;
        let signal_tag = dispatched.signal.short_tag();

        // Drain any pending fill events into a shared buffer for this cycle
        drain_fill_events(&mut fill_rx, &app);

        // ── DLS state update ──────────────────────────────────────────────
        // Feed the signal into the DLS engine before dispatch so /api/status
        // and downstream consumers see the latest par score. Pure state —
        // does not influence trade decisions yet, so we deliberately do NOT
        // push to the events ribbon (would otherwise log a "dls" line on
        // every ball, drowning out the trade events).
        {
            let mut dls = app.dls.write().unwrap();
            dls.apply(&signal);
            tracing::debug!(dls = %dls.describe(), "dls updated");
        }

        // ── Stale-revert dispatch ──────────────────────────────────────────
        // For trade-triggering signals, decide whether to fire a fresh trade
        // (NORMAL), skip because pending reverts fight the new direction
        // (WAIT), or re-price the pending reverts at current book (AUGMENT).
        // See helpers near top of file for the rule definitions.
        let new_direction = signal.trade_direction();

        if let Some(dir) = new_direction {
            match decide_dispatch_action(&app, state.batting, dir) {
                DispatchAction::Wait => {
                    let n = app.pending_revert_count();
                    let msg = format!(
                        "event{event_seq} [{signal_tag}]: WAIT — {n} pending revert(s) oppose new direction"
                    );
                    tracing::info!("{msg}");
                    app.push_event("dispatch", &msg);
                    if let Some(ref db) = *app.db.read().unwrap() {
                        db.update_oracle_event_decision(event_seq, "WAIT");
                    }
                    // Surface in the per-signal panel so the user sees why no FAK fired.
                    app.open_skipped_signal_group(
                        format!("{event_seq}-{signal_tag}"),
                        event_seq, &signal_tag, &signal_label_for_panel(&signal),
                        config.team_name(state.batting), config.team_name(state.bowling()),
                        crate::state::GroupOutcome::Wait,
                        &format!("WAIT — {n} pending revert(s) oppose"),
                    );
                    continue;
                }
                DispatchAction::Augment => {
                    let n = app.pending_revert_count();
                    let msg = format!(
                        "event{event_seq} [{signal_tag}]: AUGMENT — re-pricing {n} pending revert(s)"
                    );
                    tracing::info!("{msg}");
                    app.push_event("dispatch", &msg);
                    if let Some(ref db) = *app.db.read().unwrap() {
                        db.update_oracle_event_decision(event_seq, "AUGMENT");
                    }
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
                    if let Some(ref db) = *app.db.read().unwrap() {
                        db.update_oracle_event_decision(event_seq, "NORMAL");
                    }
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

                // D2: refuse to trade if the market WS is down or hasn't
                // delivered a snapshot since the last (re)connect. A
                // connected, snapshotted feed keeps the local book current
                // via deltas — quiet markets are fine. See `crate::ws_health`.
                if let Some(reason) = ws_blocks_signal(&app) {
                    let msg = format!("{reason} — wicket trade skipped");
                    tracing::warn!("{msg}");
                    app.push_event("skip", &msg);
                    if let Some(ref db) = *app.db.read().unwrap() {
                        db.update_oracle_event_decision(event_seq, reason);
                    }
                    continue;
                }

                if !price_in_safe_range(&config, &books) {
                    let (min, max) = config.safe_price_range();
                    let msg = format!("price outside {min}-{max} safe range — skipping trade");
                    tracing::info!("{msg}");
                    app.push_event("skip", &msg);
                    continue;
                }

                // Record signal-to-decision latency
                app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());

                // Decide directional vs reverse entry based on pre-signal
                // book move. WICKET legs default to SELL batting + BUY bowling;
                // reverse swaps to BUY batting + SELL bowling.
                let direction = match resolve_entry_plan_for(
                    &app, &config, LegPair::SellBattingBuyBowling, "WICKET", batting, &books,
                ) {
                    EntryPlan::SkipDelayed => {
                        let msg = "WICKET: DELAYED — market pre-moved before signal — trade skipped".to_string();
                        tracing::warn!("{msg}");
                        app.push_event("skip", &msg);
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.update_oracle_event_decision(event_seq, "DELAYED_SKIP");
                        }
                        continue;
                    }
                    EntryPlan::Trade(d) => d,
                };
                if let Some(ref db) = *app.db.read().unwrap() {
                    db.update_oracle_event_decision(event_seq, dispatch_decision_tag(direction));
                }
                let (sell_team, buy_team) = entry_legs(LegPair::SellBattingBuyBowling, batting, direction);
                let (sell_book, _) = team_books(&books, sell_team);
                let (buy_book, _) = team_books(&books, buy_team);

                // Try pre-signed cache first, fall back to fresh build
                let sell_order = if app.is_team_enabled(sell_team) {
                    let held = position.lock().unwrap().token_balance(sell_team);
                    resolve_sell_order(&config, &app, auth, sell_team, &sell_book, held, signal_time).await
                } else {
                    tracing::debug!(team = %config.team_name(sell_team), "team disabled — sell skipped");
                    None
                };
                let buy_order = if app.is_team_enabled(buy_team) {
                    resolve_buy_order(&config, &app, auth, buy_team, &buy_book, signal_time).await
                } else {
                    tracing::debug!(team = %config.team_name(buy_team), "team disabled — buy skipped");
                    None
                };

                if sell_order.is_none() {
                    let msg = format!("no bid on {} — sell leg skipped", config.team_name(sell_team));
                    tracing::warn!("{msg}");
                    app.push_event("warn", &msg);
                }
                if buy_order.is_none() {
                    let msg = format!("no ask on {} — buy leg skipped", config.team_name(buy_team));
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
                    if let Some(reason) = ws_blocks_signal(&app) {
                        let msg = format!("RUN{r}: {reason} — boundary skipped");
                        tracing::warn!("{msg}");
                        app.push_event("skip", &msg);
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.update_oracle_event_decision(event_seq, reason);
                        }
                    } else if price_in_safe_range(&config, &books) {
                        app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());
                        let label = format!("RUN{r}");
                        let direction = match resolve_entry_plan_for(
                            &app, &config, LegPair::SellBowlingBuyBatting, &label, state.batting, &books,
                        ) {
                            EntryPlan::SkipDelayed => {
                                let msg = format!("{label}: DELAYED — market pre-moved before signal — boundary skipped");
                                tracing::warn!("{msg}");
                                app.push_event("skip", &msg);
                                if let Some(ref db) = *app.db.read().unwrap() {
                                    db.update_oracle_event_decision(event_seq, "DELAYED_SKIP");
                                }
                                continue;
                            }
                            EntryPlan::Trade(d) => d,
                        };
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.update_oracle_event_decision(event_seq, dispatch_decision_tag(direction));
                        }
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "RUN", direction, event_seq, signal_tag.clone(), signal_time).await;
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
                    if let Some(reason) = ws_blocks_signal(&app) {
                        let msg = format!("WD{r}: {reason} — boundary skipped");
                        tracing::warn!("{msg}");
                        app.push_event("skip", &msg);
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.update_oracle_event_decision(event_seq, reason);
                        }
                    } else if price_in_safe_range(&config, &books) {
                        app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());
                        let label = format!("WD{r}");
                        let direction = match resolve_entry_plan_for(
                            &app, &config, LegPair::SellBowlingBuyBatting, &label, state.batting, &books,
                        ) {
                            EntryPlan::SkipDelayed => {
                                let msg = format!("{label}: DELAYED — market pre-moved before signal — boundary skipped");
                                tracing::warn!("{msg}");
                                app.push_event("skip", &msg);
                                if let Some(ref db) = *app.db.read().unwrap() {
                                    db.update_oracle_event_decision(event_seq, "DELAYED_SKIP");
                                }
                                continue;
                            }
                            EntryPlan::Trade(d) => d,
                        };
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.update_oracle_event_decision(event_seq, dispatch_decision_tag(direction));
                        }
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "WD", direction, event_seq, signal_tag.clone(), signal_time).await;
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
                    if let Some(reason) = ws_blocks_signal(&app) {
                        let msg = format!("NB{r}: {reason} — boundary skipped");
                        tracing::warn!("{msg}");
                        app.push_event("skip", &msg);
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.update_oracle_event_decision(event_seq, reason);
                        }
                    } else if price_in_safe_range(&config, &books) {
                        app.latency.record(LatencyMetric::SignalToDecision, signal_time.elapsed());
                        let label = format!("NB{r}");
                        let direction = match resolve_entry_plan_for(
                            &app, &config, LegPair::SellBowlingBuyBatting, &label, state.batting, &books,
                        ) {
                            EntryPlan::SkipDelayed => {
                                let msg = format!("{label}: DELAYED — market pre-moved before signal — boundary skipped");
                                tracing::warn!("{msg}");
                                app.push_event("skip", &msg);
                                if let Some(ref db) = *app.db.read().unwrap() {
                                    db.update_oracle_event_decision(event_seq, "DELAYED_SKIP");
                                }
                                continue;
                            }
                            EntryPlan::Trade(d) => d,
                        };
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.update_oracle_event_decision(event_seq, dispatch_decision_tag(direction));
                        }
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "NB", direction, event_seq, signal_tag.clone(), signal_time).await;
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

    // B5: stable id that joins the FAK pair, the revert, and any AUGMENTs.
    let correlation_id = format!("{event_seq}-{signal_tag}");

    // Capture intended sizes before fire_fak_batch consumes the orders.
    let intended_sell = sell_order.as_ref().map(|o| (o.fak.team, o.fak.price, o.fak.size));
    let intended_buy = buy_order.as_ref().map(|o| (o.fak.team, o.fak.price, o.fak.size));

    // Open the signal group. SELL/BUY legs go Pending if we have an order to
    // post; NotPlanned otherwise. Revert legs always start NotPlanned —
    // they're only planned once the matching FAK fills.
    let (group_batting, group_bowling) = match label {
        // Wicket favours bowling: SELL leg is on batting team, BUY on bowling.
        "WICKET" => (
            intended_sell.map(|(t, _, _)| config.team_name(t).to_string()).unwrap_or_default(),
            intended_buy.map(|(t, _, _)| config.team_name(t).to_string()).unwrap_or_default(),
        ),
        // Boundary favours batting: SELL leg is on bowling, BUY on batting.
        _ => (
            intended_buy.map(|(t, _, _)| config.team_name(t).to_string()).unwrap_or_default(),
            intended_sell.map(|(t, _, _)| config.team_name(t).to_string()).unwrap_or_default(),
        ),
    };
    app.open_signal_group(crate::state::SignalGroup {
        correlation_id: correlation_id.clone(),
        event_seq,
        signal_tag: signal_tag.clone(),
        label: label.to_string(),
        ts_ist: crate::state::ist_now(),
        batting: group_batting,
        bowling: group_bowling,
        legs: vec![
            crate::state::LegStatus {
                role: crate::state::LegRole::FakSell,
                state: if intended_sell.is_some() {
                    crate::state::LegState::Pending
                } else {
                    crate::state::LegState::NotPlanned { reason: "sell skipped at gate".into() }
                },
            },
            crate::state::LegStatus {
                role: crate::state::LegRole::FakBuy,
                state: if intended_buy.is_some() {
                    crate::state::LegState::Pending
                } else {
                    crate::state::LegState::NotPlanned { reason: "buy skipped at gate".into() }
                },
            },
            crate::state::LegStatus {
                role: crate::state::LegRole::RevertBuy,
                state: crate::state::LegState::NotPlanned { reason: "awaits SELL fill".into() },
            },
            crate::state::LegStatus {
                role: crate::state::LegRole::RevertSell,
                state: crate::state::LegState::NotPlanned { reason: "awaits BUY fill".into() },
            },
        ],
        total_fee_paid: Decimal::ZERO,
        net_pnl: None,
        outcome: crate::state::GroupOutcome::Open,
    });

    let (sell_result, buy_result) = fire_fak_batch(
        config, auth, position, app,
        sell_order, &sell_tag,
        buy_order,  &buy_tag,
        &correlation_id,
        signal_time,
    ).await;

    // Promote each FAK leg from Pending → Posted (or Rejected if the batch
    // didn't return an order_id).
    if let Some((_, price, size)) = intended_sell {
        let oid = sell_result.as_ref().and_then(|r| r.order_id.clone());
        app.update_leg(&correlation_id, crate::state::LegRole::FakSell, |leg| {
            leg.state = match oid {
                Some(o) => crate::state::LegState::Posted { order_id: o, price, size },
                None => crate::state::LegState::Rejected { reason: "FAK rejected at submit".into() },
            };
        });
    }
    if let Some((_, price, size)) = intended_buy {
        let oid = buy_result.as_ref().and_then(|r| r.order_id.clone());
        app.update_leg(&correlation_id, crate::state::LegRole::FakBuy, |leg| {
            leg.state = match oid {
                Some(o) => crate::state::LegState::Posted { order_id: o, price, size },
                None => crate::state::LegState::Rejected { reason: "FAK rejected at submit".into() },
            };
        });
    }

    let poll_interval = Duration::from_millis(config.fill_poll_interval_ms.max(200));
    let poll_timeout  = Duration::from_millis(config.fill_poll_timeout_ms);

    // Detect fills: race user WS events against REST polling (no hardcoded 3.5s sleep)
    let (sell_fill, buy_fill) = tokio::join!(
        detect_fill(auth, app, sell_result, poll_interval, poll_timeout, config, signal_time),
        detect_fill(auth, app, buy_result,  poll_interval, poll_timeout, config, signal_time),
    );

    // V2 platform fee per FAK fill, in USDC. FAK orders are takers by intent
    // (`build_*_order` prices them at L+1 to cross the book) — the matching
    // engine charges the fee on every fill. `fee_usdc_sell` returns the fee
    // expressed in USDC for both BUY and SELL since
    // `fee_tokens_buy · p == fee_usdc_sell`.
    let fee_e = crate::fees::fee_exponent_as_u32(config.fee_exponent);
    let mut sell_entry_fee = Decimal::ZERO;
    let mut buy_entry_fee = Decimal::ZERO;

    if let Some(ref f) = sell_fill {
        let cost = f.filled_size * f.avg_price;
        let fee = crate::fees::fee_usdc_sell(f.filled_size, f.avg_price, config.fee_rate, fee_e);
        sell_entry_fee = fee;
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size });
        let msg = format!("{label}: SELL {} {} @ {} = ${} filled (fee ${})",
            f.filled_size, config.team_name(f.order.team), f.avg_price,
            cost.round_dp(2), fee.round_dp(4));
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
            fee,
        });
        // Mark FAK_SELL leg as Filled (or Partial vs requested).
        let requested = intended_sell.map(|(_, _, s)| s).unwrap_or(f.filled_size);
        let oid = f.order_id.clone();
        let filled_size = f.filled_size;
        let avg_price = f.avg_price;
        app.update_leg(&correlation_id, crate::state::LegRole::FakSell, |leg| {
            leg.state = if filled_size >= requested {
                crate::state::LegState::Filled { order_id: oid, size: filled_size, avg_price, fee }
            } else {
                crate::state::LegState::Partial { order_id: oid, filled: filled_size, requested, avg_price, fee }
            };
        });
    } else if intended_sell.is_some() {
        // Posted but no fill within the timeout → killed.
        app.update_leg(&correlation_id, crate::state::LegRole::FakSell, |leg| {
            if let crate::state::LegState::Posted { order_id, .. } = &leg.state {
                let oid = order_id.clone();
                leg.state = crate::state::LegState::Killed { order_id: oid };
            }
        });
    }
    if let Some(ref f) = buy_fill {
        let cost = f.filled_size * f.avg_price;
        let fee = crate::fees::fee_usdc_sell(f.filled_size, f.avg_price, config.fee_rate, fee_e);
        buy_entry_fee = fee;
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size });
        let msg = format!("{label}: BUY {} {} @ {} = ${} filled (fee ${})",
            f.filled_size, config.team_name(f.order.team), f.avg_price,
            cost.round_dp(2), fee.round_dp(4));
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
            fee,
        });
        let requested = intended_buy.map(|(_, _, s)| s).unwrap_or(f.filled_size);
        let oid = f.order_id.clone();
        let filled_size = f.filled_size;
        let avg_price = f.avg_price;
        app.update_leg(&correlation_id, crate::state::LegRole::FakBuy, |leg| {
            leg.state = if filled_size >= requested {
                crate::state::LegState::Filled { order_id: oid, size: filled_size, avg_price, fee }
            } else {
                crate::state::LegState::Partial { order_id: oid, filled: filled_size, requested, avg_price, fee }
            };
        });
    } else if intended_buy.is_some() {
        app.update_leg(&correlation_id, crate::state::LegRole::FakBuy, |leg| {
            if let crate::state::LegState::Posted { order_id, .. } = &leg.state {
                let oid = order_id.clone();
                leg.state = crate::state::LegState::Killed { order_id: oid };
            }
        });
    }
    // After FAK results land — if no reverts will be posted, this also closes
    // the group early.
    app.mark_group_closed_if_terminal(&correlation_id);
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

    // Use cached tick_size from background task (no HTTP in hot path).
    // D8: helper warns if both the cache and config are unparseable.
    let tick: Decimal = app.resolve_tick_size(&config.tick_size);

    let edge_ticks = edge_ticks_for_label(label, &config);
    let edge_amount = crate::fees::dec_from_f64(edge_ticks) * tick;

    tracing::info!(delay_ms = config.revert_delay_ms, edge_ticks, tick = %tick, edge_amount = %edge_amount, "{label} REVERT");
    app.push_event("revert", &format!("{label}: revert after {}ms (edge {edge_ticks} ticks = {edge_amount})", config.revert_delay_ms));

    // Determine tick precision for rounding (e.g., 0.01 → 2dp, 0.001 → 3dp).
    // Capped at 6 because if `tick` is corrupted by an f64 round-trip it
    // could be `0.0100000000000000002...` (28 decimals), which would
    // propagate noise into every limit price (`0.21000000…00043…`). 6 dp
    // is finer than any real tick we'll see.
    let tick_dp = tick_decimal_places(tick);

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
        if let Some(oid) = execute_limit(
            &config, auth, &revert_order, position, "REVERT_SELL", app,
            &correlation_id, "REVERT_GTC", None,
        ).await {
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
                correlation_id: correlation_id.clone(),
                entry_fee: buy_entry_fee,
            });
            // Promote RevertSell leg from NotPlanned → Posted.
            let oid_for_state = oid.clone();
            app.update_leg(&correlation_id, crate::state::LegRole::RevertSell, |leg| {
                leg.state = crate::state::LegState::Posted {
                    order_id: oid_for_state, price: limit_price, size,
                };
            });
            spawn_revert_fill_monitor(
                config.clone(), auth.clone(), app.clone(), position.clone(),
                oid, f.order.team, Side::Sell, size, limit_price, revert_label,
                correlation_id.clone(), crate::state::LegRole::RevertSell,
            );
        } else {
            app.update_leg(&correlation_id, crate::state::LegRole::RevertSell, |leg| {
                leg.state = crate::state::LegState::Rejected { reason: "GTC post failed".into() };
            });
            app.mark_group_closed_if_terminal(&correlation_id);
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
        if let Some(oid) = execute_limit(
            &config, auth, &revert_order, position, "REVERT_BUY", app,
            &correlation_id, "REVERT_GTC", None,
        ).await {
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
                correlation_id: correlation_id.clone(),
                entry_fee: sell_entry_fee,
            });
            let oid_for_state = oid.clone();
            app.update_leg(&correlation_id, crate::state::LegRole::RevertBuy, |leg| {
                leg.state = crate::state::LegState::Posted {
                    order_id: oid_for_state, price: limit_price, size,
                };
            });
            spawn_revert_fill_monitor(
                config.clone(), auth.clone(), app.clone(), position.clone(),
                oid, f.order.team, Side::Buy, size, limit_price, revert_label,
                correlation_id.clone(), crate::state::LegRole::RevertBuy,
            );
        } else {
            app.update_leg(&correlation_id, crate::state::LegRole::RevertBuy, |leg| {
                leg.state = crate::state::LegState::Rejected { reason: "GTC post failed".into() };
            });
            app.mark_group_closed_if_terminal(&correlation_id);
        }
    }
}

/// Spawn a background task that polls a revert GTC order until it fills.
///
/// The monitor runs until one of:
///   - A fill is observed (WS event buffer or REST poll) → position updated,
///     revert removed, task exits.
///   - The order reaches a terminal non-fill status (cancelled/expired) → revert
///     removed, task exits.
///   - The revert is removed externally (e.g., perform_augment cancelled it on
///     a new opposite signal) → task exits silently.
///   - The server cancellation token fires → task exits on next poll boundary.
///
/// No wall-clock timeout. Per the stale-revert dispatch design, reverts are
/// tracked indefinitely; if the market never reaches the limit price during
/// the match, pending reverts stay pending across innings/match-over and are
/// cleaned up by `reset_for_new_match` at the start of the next match.
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
    correlation_id: String,
    leg_role: crate::state::LegRole,
) {
    tokio::spawn(async move {
        // Tighter poll cadence when the timeout is armed so we hit the
        // configured deadline within ~1s rather than ~5s. With timeout=0
        // we keep the original 5s for cheaper long-term tracking.
        let revert_timeout_ms = config.revert_timeout_ms;
        let poll_interval = if revert_timeout_ms > 0 {
            Duration::from_secs(1)
        } else {
            Duration::from_secs(5)
        };
        // Heartbeat every 60 polls. Logs age so long-lived reverts are
        // visible and don't look like leaked tasks.
        const HEARTBEAT_EVERY: u64 = 60;
        let mut iter: u64 = 0;
        let started_at = Instant::now();

        loop {
            iter += 1;
            tokio::time::sleep(poll_interval).await;

            // Check if revert was removed (cancelled by opposite event, augment,
            // match reset, etc.) — exit silently if so. Done BEFORE the
            // timeout branch so an orphaned monitor (after `reset_for_new_match`)
            // never fires a stale cancel against the previous match's order_id.
            let still_pending = app.pending_reverts.lock().unwrap()
                .iter().any(|r| r.order_id == order_id);
            if !still_pending {
                tracing::debug!(order_id = %order_id, label = %label, "revert monitor: removed externally");
                return;
            }

            // Time-based escalation: if the configured wall-clock window
            // expires without the maker revert filling, cancel it and
            // flatten via a taker FAK. `revert_timeout_ms == 0` disables —
            // and is the production default, since flattening at a worse
            // touch typically locks in adverse-selection loss.
            if should_escalate_revert_timeout(revert_timeout_ms, started_at.elapsed()) {
                handle_revert_timeout(
                    &config, &auth, &app, &position,
                    &order_id, team, side, &label, &correlation_id, leg_role,
                ).await;
                return;
            }

            // Periodic heartbeat so long-watched reverts are visible in logs.
            if iter % HEARTBEAT_EVERY == 0 {
                let age_secs = started_at.elapsed().as_secs();
                tracing::info!(
                    order_id = %order_id,
                    label = %label,
                    age_secs,
                    "[REVERT-MONITOR] still watching"
                );
            }

            // Check WS fill buffer first (fast path)
            if let Some(fill) = app.take_fill_event(&order_id) {
                let filled = fill.filled_size;
                let price = if fill.avg_price.is_zero() { limit_price } else { fill.avg_price };
                let cost = filled * price;
                // GTC reverts are makers when posted normally. With
                // `takers_only_fees=true` (enforced at startup) maker fee = 0.
                let exit_fee = revert_exit_fee(&config, filled, price);
                {
                    let mut pos = position.lock().unwrap();
                    pos.on_fill(&FakOrder { team, side, price, size: filled });
                }
                let msg = format!("{label}: REVERT FILLED {} {} @ {} = ${} (fee ${})",
                    side, config.team_name(team), price, cost.round_dp(2), exit_fee.round_dp(4));
                tracing::info!("{msg}");
                app.push_event("filled", &msg);
                let record = TradeRecord {
                    ts: crate::state::ist_now(),
                    side: format!("{side}"),
                    team: config.team_name(team).to_string(),
                    size: filled,
                    price,
                    cost,
                    order_type: "GTC".into(),
                    label: label.clone(),
                    order_id: order_id.clone(),
                    fee: exit_fee,
                };
                // D7: pair the exit trade with the round-trip write atomically.
                if let Some(revert) = app.remove_revert(&order_id) {
                    log_revert_fill_with_round_trip(&app, &config, &revert, price, filled, &order_id, record);
                } else {
                    app.log_trade(record);
                }
                if let Some(ref db) = *app.db.read().unwrap() {
                    db.mark_clob_order_terminal(&order_id, "matched", &filled.to_string());
                }
                // Update signal-group: revert leg → Filled. Group closes when
                // every leg is terminal.
                let oid_for_state = order_id.clone();
                app.update_leg(&correlation_id, leg_role, |leg| {
                    leg.state = crate::state::LegState::Filled {
                        order_id: oid_for_state, size: filled, avg_price: price, fee: exit_fee,
                    };
                });
                app.mark_group_closed_if_terminal(&correlation_id);
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
                        let exit_fee = revert_exit_fee(&config, filled, price);
                        {
                            let mut pos = position.lock().unwrap();
                            pos.on_fill(&FakOrder { team, side, price, size: filled });
                        }
                        let msg = format!("{label}: REVERT FILLED {} {} @ {} = ${} (fee ${})",
                            side, config.team_name(team), price, cost.round_dp(2), exit_fee.round_dp(4));
                        tracing::info!("{msg}");
                        app.push_event("filled", &msg);
                        let record = TradeRecord {
                            ts: crate::state::ist_now(),
                            side: format!("{side}"),
                            team: config.team_name(team).to_string(),
                            size: filled,
                            price,
                            cost,
                            order_type: "GTC".into(),
                            label: label.clone(),
                            order_id: order_id.clone(),
                            fee: exit_fee,
                        };
                        // D7: same atomic pairing as the WS path.
                        if let Some(revert) = app.remove_revert(&order_id) {
                            log_revert_fill_with_round_trip(&app, &config, &revert, price, filled, &order_id, record);
                        } else {
                            app.log_trade(record);
                        }
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.mark_clob_order_terminal(&order_id, "matched", &filled.to_string());
                        }
                        let oid_for_state = order_id.clone();
                        app.update_leg(&correlation_id, leg_role, |leg| {
                            leg.state = crate::state::LegState::Filled {
                                order_id: oid_for_state, size: filled, avg_price: price, fee: exit_fee,
                            };
                        });
                        app.mark_group_closed_if_terminal(&correlation_id);
                        app.snapshot_inventory();
                        return;
                    }

                    if open_order.is_terminal() && filled.is_zero() {
                        tracing::info!(order_id = %order_id, status, "revert order terminal with no fill");
                        app.push_event("warn", &format!("{label}: revert {status} (no fill)"));
                        app.remove_revert(&order_id);
                        if let Some(ref db) = *app.db.read().unwrap() {
                            db.mark_clob_order_terminal(&order_id, &status, "0");
                        }
                        let oid_for_state = order_id.clone();
                        let reason_for_state = status.clone();
                        app.update_leg(&correlation_id, leg_role, |leg| {
                            leg.state = crate::state::LegState::Cancelled {
                                order_id: oid_for_state, reason: reason_for_state,
                            };
                        });
                        app.mark_group_closed_if_terminal(&correlation_id);
                        return;
                    }
                }
                Err(_) => {} // not indexed yet, keep polling
            }
        }
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
                // Flip each cancelled revert to terminal in our DB so the
                // open-orders view stops showing them as live.
                if let Some(ref db) = *app.db.read().unwrap() {
                    for oid in &order_ids {
                        db.mark_clob_order_terminal(oid, "cancelled", "0");
                    }
                }
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
    // D8: same fallback warning as the FAK-revert path.
    let tick: Decimal = app.resolve_tick_size(&config.tick_size);
    let tick_dp = tick_decimal_places(tick);

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
            let exit_fee = revert_exit_fee(config, filled_size, avg_price);
            let record = TradeRecord {
                ts: crate::state::ist_now(),
                side: format!("{}", stale_rev.side),
                team: config.team_name(stale_rev.team).to_string(),
                size: filled_size,
                price: avg_price,
                cost,
                order_type: "GTC".into(),
                label: stale_rev.label.clone(),
                order_id: stale_rev.order_id.clone(),
                fee: exit_fee,
            };
            // D7: atomic exit-trade + round-trip on AUGMENT capture path.
            // The PendingRevert was already popped from `app.pending_reverts`
            // earlier in `take_reverts_for_team`, so we don't `remove_revert`
            // again here — pass it directly.
            log_revert_fill_with_round_trip(
                app, config, &stale_rev, avg_price, filled_size, &stale_rev.order_id, record,
            );
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
        let edge_amount = crate::fees::dec_from_f64(edge_ticks) * tick;

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

        // B5: AUGMENT'd revert inherits the original revert's correlation_id
        // so all rows for the original ball stay joinable. The new event_seq
        // is recorded separately on the row's `replaces_order_id` chain — i.e.
        // "this augment was triggered by a later signal but is still part of
        // the original signal's order group".
        let inherited_correlation_id = stale_rev.correlation_id.clone();
        let prior_oid = stale_rev.order_id.clone();
        // Map revert side → SignalGroup leg role. SELL revert closes a BUY
        // FAK (RevertSell); BUY revert closes a SELL FAK (RevertBuy).
        let leg_role = match stale_rev.side {
            Side::Sell => crate::state::LegRole::RevertSell,
            Side::Buy => crate::state::LegRole::RevertBuy,
        };
        if let Some(new_oid) = execute_limit(
            config, auth, &repost_order, position, &augment_label, app,
            &inherited_correlation_id, "REVERT_GTC", Some(&prior_oid),
        ).await {
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
                correlation_id: inherited_correlation_id.clone(),
                // Inherit entry-leg fee from the cancelled prior revert so
                // round-trip net_pnl stays correct even after one or more
                // AUGMENT reposts.
                entry_fee: stale_rev.entry_fee,
            });
            // Update the original group's leg state: cancelled prior
            // → reposted as new. The leg is back in `Posted`; the AUGMENT
            // history is in `clob_orders.replaces_order_id`.
            let oid_for_state = new_oid.clone();
            app.update_leg(&inherited_correlation_id, leg_role, |leg| {
                leg.state = crate::state::LegState::Posted {
                    order_id: oid_for_state,
                    price: new_price,
                    size: remaining,
                };
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
                inherited_correlation_id.clone(),
                leg_role,
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
                                // Prefer the JSON string form when present —
                                // exact decimal preserved. Fall back to the
                                // safe shortest-roundtrip f64 conversion so a
                                // numeric `0.01` doesn't smuggle its binary
                                // representation (0.0100000000000000002...)
                                // into the cached tick.
                                let tick_opt: Option<Decimal> = match &market["orderPriceMinTickSize"] {
                                    serde_json::Value::String(s) => s.parse().ok(),
                                    serde_json::Value::Number(_) => market["orderPriceMinTickSize"]
                                        .as_f64()
                                        .map(crate::fees::dec_from_f64),
                                    _ => None,
                                };
                                if let Some(tick) = tick_opt {
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
                    Err(e) => {
                        tracing::debug!(error = %e, "[TICK-REFRESH] fetch failed");
                    }
                }
            }
        }
    }
}

/// D7 (TODO.md): atomic exit-trade + round-trip write.
///
/// Replaces the previous `app.log_trade(...)` followed by `log_round_trip(...)`
/// pairing — the two DB inserts now happen inside one SQLite transaction so
/// a crash between them no longer leaves the ledger half-finished. The
/// in-memory `trade_log` push and the `"pnl"` event still fire as before.
///
/// Dry-run trades skip the DB write entirely (matching the previous behavior
/// of `AppState::log_trade`).
fn log_revert_fill_with_round_trip(
    app: &Arc<AppState>,
    config: &Config,
    revert: &PendingRevert,
    exit_price: Decimal,
    exit_size: Decimal,
    exit_order_id: &str,
    trade_record: TradeRecord,
) {
    // Round-trip PnL — gross, no fees deducted yet.
    let pnl = match revert.side {
        Side::Sell => (exit_price - revert.entry_price) * exit_size, // entry was BUY, exit is SELL
        Side::Buy => (revert.entry_price - exit_price) * exit_size,  // entry was SELL, exit is BUY
    };
    let entry_side = match revert.side {
        Side::Sell => "BUY",
        Side::Buy => "SELL",
    };

    // Net PnL = gross PnL − entry-leg fee − exit-leg fee. `entry_fee` was
    // computed and stashed at FAK-fill time. `exit_fee` for a maker revert is
    // zero under `fd.to=true`, but we read it from the trade_record so the
    // edge case (post-only failure causes a crossing GTC) eventually shows up.
    let fee_in = revert.entry_fee;
    let fee_out = trade_record.fee;
    let net_pnl = pnl - fee_in - fee_out;

    tracing::info!(
        team = %config.team_name(revert.team),
        entry_side, entry_price = %revert.entry_price,
        exit_price = %exit_price, size = %exit_size,
        pnl = %pnl.round_dp(4),
        fee_in = %fee_in.round_dp(4),
        fee_out = %fee_out.round_dp(4),
        net_pnl = %net_pnl.round_dp(4),
        label = %revert.label,
        "ROUND-TRIP complete"
    );
    app.push_event("pnl", &format!("{}: {} {} entry={} exit={} sz={} PnL=${} fees=${} net=${}",
        revert.label, entry_side, config.team_name(revert.team),
        revert.entry_price, exit_price, exit_size,
        pnl.round_dp(4), (fee_in + fee_out).round_dp(4), net_pnl.round_dp(4)));

    let is_dry = trade_record.order_id.starts_with("dry_run");
    if !is_dry {
        if let Some(ref db) = *app.db.read().unwrap() {
            let slug = config.market_slug.clone();
            let pnl_str = pnl.round_dp(6).to_string();
            let fee_in_str = fee_in.round_dp(6).to_string();
            let fee_out_str = fee_out.round_dp(6).to_string();
            let net_pnl_str = net_pnl.round_dp(6).to_string();
            let entry_ts = revert.placed_at.elapsed().as_secs().to_string();
            let exit_ts = crate::state::ist_now();
            let entry_price_str = revert.entry_price.to_string();
            let exit_price_str = exit_price.to_string();
            let exit_size_str = exit_size.to_string();
            let team_str = config.team_name(revert.team).to_string();
            let fee_str = trade_record.fee.to_string();

            db.insert_revert_fill_atomic(
                &crate::db::TradeArgs {
                    ts: &trade_record.ts,
                    side: &trade_record.side,
                    team: &trade_record.team,
                    size: &trade_record.size.to_string(),
                    price: &trade_record.price.to_string(),
                    cost: &trade_record.cost.to_string(),
                    order_type: &trade_record.order_type,
                    label: &trade_record.label,
                    order_id: &trade_record.order_id,
                    slug: &slug,
                    fee: &fee_str,
                },
                &crate::db::RoundTripArgs {
                    entry_ts: &entry_ts,
                    exit_ts: &exit_ts,
                    team: &team_str,
                    entry_side,
                    entry_price: &entry_price_str,
                    exit_price: &exit_price_str,
                    size: &exit_size_str,
                    pnl: &pnl_str,
                    label: &revert.label,
                    entry_order_id: "", // not stored on PendingRevert today
                    exit_order_id,
                    slug: &slug,
                    fee_in: &fee_in_str,
                    fee_out: &fee_out_str,
                    net_pnl: &net_pnl_str,
                },
            );
        }
    }

    // In-memory trade log is updated AFTER the DB write so /api/status reflects
    // committed state on success; on a DB failure we still keep the in-memory
    // record because the outer task already saw the fill.
    app.trade_log.lock().unwrap().push(trade_record);

    // Stamp net_pnl on the originating signal-group. Group closes once both
    // legs are terminal — the leg-state update sites already call
    // `mark_group_closed_if_terminal` after each transition.
    app.set_group_net_pnl(&revert.correlation_id, net_pnl);
}

/// Log a completed round-trip (FAK entry + GTC revert exit) to the DB.
/// PnL = exit_proceeds - entry_cost for each leg.
///
/// Kept for any future caller that does NOT have an exit `TradeRecord` to
/// pair with. The trade log itself is now written by the atomic
/// [`log_revert_fill_with_round_trip`] helper at the three known revert-fill
/// sites.
#[allow(dead_code)]
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
        // Legacy fall-back: net_pnl == pnl when no per-leg fees are wired in.
        let pnl_str = pnl.round_dp(6).to_string();
        db.insert_round_trip(
            &revert.placed_at.elapsed().as_secs().to_string(), // approximate entry time
            &now,
            &config.team_name(revert.team),
            entry_side,
            &revert.entry_price.to_string(),
            &exit_price.to_string(),
            &exit_size.to_string(),
            &pnl_str,
            &revert.label,
            "", // entry_order_id not stored in PendingRevert — could add later
            exit_order_id,
            &slug,
            "0",
            "0",
            &pnl_str,
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
                                polymarket_trade_id: None,
                                raw_json: String::new(),
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
    direction: EntryDirection,
    event_seq: u64,
    signal_tag: String,
    signal_time: Instant,
) {
    // Boundary directional: SELL bowling + BUY batting. Reverse swaps to
    // SELL batting + BUY bowling.
    let (sell_team, buy_team) = entry_legs(LegPair::SellBowlingBuyBatting, batting, direction);
    let (sell_book, _) = team_books(&books, sell_team);
    let (buy_book, _) = team_books(&books, buy_team);

    let sell_order = if app.is_team_enabled(sell_team) {
        let held = position.lock().unwrap().token_balance(sell_team);
        resolve_sell_order(&config, &app, &auth, sell_team, &sell_book, held, signal_time).await
    } else {
        None
    };
    let buy_order = if app.is_team_enabled(buy_team) {
        resolve_buy_order(&config, &app, &auth, buy_team, &buy_book, signal_time).await
    } else {
        None
    };

    if sell_order.is_none() && buy_order.is_none() {
        app.push_event("skip", &format!("{kind}{runs}: no liquidity for boundary trade"));
        return;
    }

    let msg = format!("{kind}{runs} BOUNDARY [{:?}] — sell {} buy {}",
        direction, config.team_name(sell_team), config.team_name(buy_team));
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
    correlation_id: &str,
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

    // D5 (TODO.md): inventory check for sell order. The order book may have
    // moved since `resolve_sell_order` sized the leg from `pos.token_balance`
    // — a concurrent revert fill in the same window can shrink held tokens
    // below `sell.fak.size`. Re-read at submit time and refuse the leg if
    // we no longer have enough inventory; otherwise the CLOB rejects with a
    // confusing "insufficient balance" and the strategy keeps thinking it
    // owns tokens it doesn't.
    let sell_order = if let Some(ref sell) = sell_order {
        let held = position.lock().unwrap().token_balance(sell.fak.team);
        if held < sell.fak.size {
            tracing::warn!(
                tag = sell_tag,
                team = %config.team_name(sell.fak.team),
                held = %held,
                requested = %sell.fak.size,
                "[D5] held tokens shrunk below sell size — sell skipped",
            );
            app.push_event(
                "warn",
                &format!(
                    "{sell_tag}: insufficient tokens (held {held} < {} requested), skipping",
                    sell.fak.size,
                ),
            );
            None
        } else {
            sell_order
        }
    } else {
        sell_order
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

        // B5: persist placement metadata so the FAK pair is joinable to its
        // future revert (and any augments) via correlation_id.
        if let Some(ref db) = *app.db.read().unwrap() {
            let purpose = match fak.side {
                Side::Buy => "FAK_BUY",
                Side::Sell => "FAK_SELL",
            };
            let asset_id = config.token_id(fak.team).to_string();
            let side_str = format!("{}", fak.side);
            let price_str = fak.price.to_string();
            let size_str = fak.size.to_string();
            let team_str = config.team_name(fak.team).to_string();
            let ts = crate::state::ist_now();
            db.record_order_placement(&crate::db::OrderPlacement {
                order_id: oid,
                slug: &config.market_slug,
                asset_id: &asset_id,
                side: &side_str,
                price: &price_str,
                original_size: &size_str,
                status,
                order_type: "FAK",
                created_at: &ts,
                team: &team_str,
                correlation_id,
                purpose,
                replaces_order_id: None,
            });
        }

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
            if let Some(ref db) = *app.db.read().unwrap() {
                db.mark_clob_order_terminal(order_id, "matched", &fill.filled_size.to_string());
            }
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
                    if let Some(ref db) = *app.db.read().unwrap() {
                        db.mark_clob_order_terminal(order_id, "matched", &filled.to_string());
                    }
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
                    if let Some(ref db) = *app.db.read().unwrap() {
                        db.mark_clob_order_terminal(order_id, "matched", &sz.to_string());
                    }
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
                    if let Some(ref db) = *app.db.read().unwrap() {
                        // Use the matcher's reported status verbatim so the
                        // UI shows "killed"/"cancelled"/"expired" accurately.
                        db.mark_clob_order_terminal(order_id, status, "0");
                    }
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

/// Read current WS health from `AppState`. Returns `Default` (block trading)
/// if the channel hasn't been initialised yet — safer than panicking before
/// `start_book_ws` has run.
pub(crate) fn current_ws_health(app: &AppState) -> WsHealth {
    if let Some(rx) = app.ws_health_rx.read().unwrap().as_ref() {
        *rx.borrow()
    } else {
        WsHealth::default()
    }
}

/// Block-trading guard. Returns `Some(reason)` when the strategy should skip
/// the current signal. Replaces the old `book_is_stale` timestamp check —
/// see `crate::ws_health` for the semantics.
pub(crate) fn ws_blocks_signal(app: &AppState) -> Option<&'static str> {
    ws_blocks_trading_reason(&current_ws_health(app))
}

/// Which two team↔side bindings the directional (current-strategy) entry uses
/// for a given signal type. Reverse simply swaps the bindings.
///
/// - `SellBattingBuyBowling` — wicket: directional sells the team that lost
///   wicket (its price just dropped) and buys the bowling team (price rose).
/// - `SellBowlingBuyBatting` — boundary (4/6/wide-4/no-ball-4 etc.):
///   directional sells the bowling team (price dropped) and buys the batting
///   team (price rose).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LegPair {
    SellBattingBuyBowling,
    SellBowlingBuyBatting,
}

/// Outcome of `decide_entry_direction`.
///
/// - `Directional` — execute the legs as defined by `LegPair` (current/momentum).
/// - `Reverse` — swap sell/buy team bindings (mean-reversion entry). Use when
///   the market has already moved by ≥ threshold on both sides we'd cross.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryDirection {
    Directional,
    Reverse,
}

/// Decide whether to enter directional (current strategy, momentum) or
/// reverse (mean-reversion) on a fresh signal.
///
/// Compares the current touches we'd cross against touches from `~lookback`
/// ago. Reverse only when BOTH the sell-side bid has dropped *and* the
/// buy-side ask has risen by at least `threshold`. Either side moving alone
/// is treated as book stutter, not a real news move, and stays directional.
///
/// Cold-start (no past snapshot) and one-sided book gaps both fall back to
/// directional — safer to keep current behaviour than to flip on incomplete
/// data.
///
/// `threshold` is supplied by the caller as a `Decimal` price gap so this
/// stays a pure function — caller computes `multiplier × edge_ticks × tick`.
pub fn decide_entry_direction(
    legs: LegPair,
    batting: Team,
    current: TouchSnapshot,
    past: Option<TouchSnapshot>,
    threshold: Decimal,
) -> EntryDirection {
    let past = match past {
        Some(p) => p,
        None => return EntryDirection::Directional, // cold start
    };

    // Identify which team's bid we'd hit (sell side) and which team's ask
    // we'd lift (buy side) under the *directional* leg construction. The
    // pre-signal-move check is on these two touches.
    let (sell_team, buy_team) = match legs {
        LegPair::SellBattingBuyBowling => (batting, batting.opponent()),
        LegPair::SellBowlingBuyBatting => (batting.opponent(), batting),
    };

    // News direction:
    //   sell-side bid should DROP (sell team's win-prob falling)
    //   buy-side ask should RISE (buy team's win-prob rising)
    let drop_in_sell_bid = match (past.bid(sell_team), current.bid(sell_team)) {
        (Some(p), Some(c)) => p - c,
        _ => return EntryDirection::Directional, // missing data
    };
    let rise_in_buy_ask = match (past.ask(buy_team), current.ask(buy_team)) {
        (Some(p), Some(c)) => c - p,
        _ => return EntryDirection::Directional,
    };

    if drop_in_sell_bid >= threshold && rise_in_buy_ask >= threshold {
        EntryDirection::Reverse
    } else {
        EntryDirection::Directional
    }
}

/// Resolve which (sell_team, buy_team) bindings to use for `legs` and
/// `direction`. `Directional` follows `legs` as defined; `Reverse` swaps
/// the two sides so the strategy flips from momentum-entry to mean-reversion-
/// entry (BUY the news-direction loser, SELL the news-direction winner).
pub fn entry_legs(legs: LegPair, batting: Team, direction: EntryDirection) -> (Team, Team) {
    let (sell_team, buy_team) = match legs {
        LegPair::SellBattingBuyBowling => (batting, batting.opponent()),
        LegPair::SellBowlingBuyBatting => (batting.opponent(), batting),
    };
    match direction {
        EntryDirection::Directional => (sell_team, buy_team),
        EntryDirection::Reverse => (buy_team, sell_team),
    }
}

/// `dispatch_decision` ledger tag for a direction, distinguishing the two
/// branches in post-match analysis without log archaeology. The bare
/// `"NORMAL"` write earlier in the dispatch path is overwritten by these.
pub fn dispatch_decision_tag(direction: EntryDirection) -> &'static str {
    match direction {
        EntryDirection::Directional => "NORMAL_DIRECTIONAL",
        EntryDirection::Reverse => "NORMAL_REVERSE",
    }
}

/// True when the book already moved in the news direction by ≥ `threshold`
/// on *either* leg we'd cross, over the price-history lookback window.
///
/// This is the "we're late" detector: if the market reacted before our oracle
/// delivered the signal, the edge is gone. Unlike `decide_entry_direction`
/// (which needs *both* sides to have moved before flipping to reverse), this
/// trips on *either* side — one confirmed leg move is enough to treat the
/// signal as stale.
///
/// Cold start (no past snapshot) and one-sided book gaps return `false`: with
/// no evidence of a pre-move we keep trading, mirroring
/// `decide_entry_direction`'s conservative fallback. Only moves in the news
/// direction count — an adverse/opposite move is book noise, not "we're late".
pub fn premove_blocks_entry(
    legs: LegPair,
    batting: Team,
    current: TouchSnapshot,
    past: Option<TouchSnapshot>,
    threshold: Decimal,
) -> bool {
    let past = match past {
        Some(p) => p,
        None => return false, // cold start — no pre-move evidence
    };
    let (sell_team, buy_team) = match legs {
        LegPair::SellBattingBuyBowling => (batting, batting.opponent()),
        LegPair::SellBowlingBuyBatting => (batting.opponent(), batting),
    };
    let drop_in_sell_bid = match (past.bid(sell_team), current.bid(sell_team)) {
        (Some(p), Some(c)) => p - c,
        _ => return false, // missing data — don't skip on an incomplete book
    };
    let rise_in_buy_ask = match (past.ask(buy_team), current.ask(buy_team)) {
        (Some(p), Some(c)) => c - p,
        _ => return false,
    };
    drop_in_sell_bid >= threshold || rise_in_buy_ask >= threshold
}

/// What to do with a trade-triggering signal after the pre-signal-move check.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryPlan {
    /// Trade, using this entry direction (directional vs reverse).
    Trade(EntryDirection),
    /// Skip entirely — `skip_on_premove` is enabled and the book already moved
    /// in the news direction on a leg over the lookback window (the signal
    /// arrived late). No order is placed.
    SkipDelayed,
}

/// Resolve the entry plan from `AppState` (price history, tick size) and the
/// current books. When `skip_on_premove` is enabled and the book already moved
/// on either leg over the lookback window, returns `SkipDelayed` (we're late —
/// don't trade). Otherwise returns `Trade` with the directional/reverse
/// decision. Logs the outcome for post-match review.
pub(crate) fn resolve_entry_plan_for(
    app: &Arc<AppState>,
    config: &Config,
    legs: LegPair,
    label: &str,
    batting: Team,
    books: &(OrderBook, OrderBook),
) -> EntryPlan {
    let now = Instant::now();
    let current = TouchSnapshot::from_books(now, books);
    let past = app.price_history.touches_lookback_ago(now);
    let tick_size = app.resolve_tick_size(&config.tick_size);
    let threshold = move_threshold(label, config, tick_size);

    if config.skip_on_premove && premove_blocks_entry(legs, batting, current, past, threshold) {
        tracing::info!(
            signal = label,
            threshold = %threshold,
            lookback_ms = config.move_lookback_ms,
            had_past = past.is_some(),
            "DELAYED — book pre-moved ≥ threshold on a leg; trade skipped"
        );
        return EntryPlan::SkipDelayed;
    }

    let direction = decide_entry_direction(legs, batting, current, past, threshold);
    tracing::info!(
        signal = label,
        direction = ?direction,
        threshold = %threshold,
        had_past = past.is_some(),
        "entry direction decided"
    );
    EntryPlan::Trade(direction)
}

/// Compute the price-move threshold for a given signal type.
///
/// `total_ticks = round(edge_ticks × multiplier)`; threshold in price units
/// is `total_ticks × tick_size`. Rounding to whole ticks keeps the threshold
/// on the same lattice as the book itself (no fractional-tick comparisons).
pub fn move_threshold(label: &str, config: &Config, tick_size: Decimal) -> Decimal {
    let edge_ticks = edge_ticks_for_label(label, config);
    let total = (edge_ticks * config.move_threshold_multiplier).round();
    let total_i = total.max(0.0) as i64;
    Decimal::from(total_i) * tick_size
}

/// Pure predicate for the revert monitor's wall-clock timeout escalation.
///
/// Returns `true` when the GTC revert has waited past `timeout_ms` and the
/// monitor should cancel + flatten via a taker FAK. Convention: `timeout_ms == 0`
/// disables escalation entirely — the GTC waits indefinitely.
///
/// The default in production is `0` (no escalation). Adverse-selection on the
/// revert leg costs more than holding inventory; better to wait for the
/// counter-leg fill (or close the position at innings end) than to flatten
/// at a worse touch when the market has moved against us.
pub fn should_escalate_revert_timeout(timeout_ms: u64, elapsed: std::time::Duration) -> bool {
    if timeout_ms == 0 {
        return false;
    }
    elapsed >= std::time::Duration::from_millis(timeout_ms)
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

/// Build a BUY FAK at L+1 price, sized from budget and grossed-up so that
/// after the V2 platform fee deduction we receive at least the budgeted
/// quantity of tokens.
///
/// Sizing:
/// ```text
///   net   = floor(max_trade_usdc / price)         # tokens we want net of fee
///   gross = fees::gross_up_buy(net, price, r, e)  # tokens we request
/// ```
/// `r=0` short-circuits to `gross = net`, preserving pre-fee behaviour for
/// all callers that haven't fetched a market yet.
pub(crate) fn build_buy_order(config: &Config, team: Team, book: &OrderBook) -> Option<FakOrder> {
    let levels = &book.asks.levels;
    if levels.is_empty() { return None; }

    // Price at L+1 if available, else L0
    let price = levels.get(1).map_or(levels[0].price, |l| l.price);

    // Net target — what we want after the platform fee comes out of the
    // received tokens.
    let net = (config.max_trade_usdc / price).floor();

    // Gross-up by the V2 fee factor. With r=0 this is a no-op.
    let e_u32 = crate::fees::fee_exponent_as_u32(config.fee_exponent);
    let gross = crate::fees::gross_up_buy(net, price, config.fee_rate, e_u32);

    let size = if gross < config.order_min_size {
        config.order_min_size
    } else {
        gross
    };

    Some(FakOrder { team, side: Side::Buy, price, size })
}

/// Time-based escalation: a maker revert hasn't filled within
/// `config.revert_timeout_ms`. Cancel it, determine how much filled before
/// the cancel landed, and flatten the residual via a taker FAK at L+1 of
/// the opposite side.
///
/// Always idempotent: safe to call once per revert, no double-cancel.
/// Sizing uses `compute_taker_exit_size` for `Decimal` precision; the
/// max-of-(cancel_response, get_order) handles the race where a partial
/// fill landed mid-cancel.
#[allow(clippy::too_many_arguments)]
async fn handle_revert_timeout(
    config: &Config,
    auth: &ClobAuth,
    app: &Arc<AppState>,
    position: &Position,
    order_id: &str,
    team: Team,
    side: Side,
    label: &str,
    correlation_id: &str,
    leg_role: crate::state::LegRole,
) {
    tracing::info!(
        order_id, label, timeout_ms = config.revert_timeout_ms,
        "[REVERT-TIMEOUT] firing taker exit",
    );
    app.push_event(
        "timeout",
        &format!("{}: revert timeout — cancelling GTC and firing taker exit", label),
    );

    // 1. Cancel the GTC. Dry-run skips the HTTP and assumes zero fills.
    if !config.dry_run {
        // Flip the DB row to "cancelled" up front so the open-orders view
        // stops showing the GTC as live within ~1s; if the cancel HTTP
        // fails below we won't recover, but the row was about to be
        // flipped by the polling sync anyway.
        if let Some(ref db) = *app.db.read().unwrap() {
            db.mark_clob_order_terminal(order_id, "cancelled", "0");
        }
        if let Err(e) = orders::cancel_orders_batch(auth, &[order_id.to_string()]).await {
            tracing::warn!(order_id, label, error = %e, "timeout: cancel failed — aborting taker exit");
            app.push_event("error", &format!("{}: timeout cancel failed: {}", label, e));
            // Leave the leg in Posted; the user can retry / cancel manually.
            return;
        }
    }

    // 2. Determine pre-cancel filled size — max of any signal we have.
    //    `cancel_orders_batch` doesn't return per-order matched size today,
    //    so we read it via `get_order` and also drain any buffered WS fill.
    //    The MAX wins because `size_matched` is monotonically non-decreasing
    //    on Polymarket's matcher, so the larger of the two is authoritative.
    //
    //    Note: we drain the WS buffer here even if the value is zero, so
    //    a fill that lands AFTER this check but before the FAK exit posts
    //    is still readable by the next monitor task — but the same buffer
    //    is shared and can be re-read; for the *current* timeout branch
    //    we operate strictly on what's visible right now.
    let ws_filled_pre_cancel = app.take_fill_event(order_id)
        .map(|e| e.filled_size)
        .unwrap_or(Decimal::ZERO);
    let rest_filled = if !config.dry_run {
        orders::get_order(auth, order_id).await
            .map(|o| o.filled_size())
            .unwrap_or(Decimal::ZERO)
    } else {
        Decimal::ZERO
    };
    let maker_filled = ws_filled_pre_cancel.max(rest_filled);
    tracing::info!(
        order_id, label,
        ws_filled_pre_cancel = %ws_filled_pre_cancel, rest_filled = %rest_filled,
        maker_filled = %maker_filled,
        "timeout: pre-exit fill state",
    );

    // 3. Pull the PendingRevert (entry size, entry_fee, entry_price).
    let revert = match app.remove_revert(order_id) {
        Some(r) => r,
        None => {
            tracing::debug!(order_id, label, "timeout: revert already removed (race) — exiting");
            app.update_leg(correlation_id, leg_role, |leg| {
                leg.state = crate::state::LegState::Cancelled {
                    order_id: order_id.to_string(),
                    reason: "timeout: removed externally".into(),
                };
            });
            app.mark_group_closed_if_terminal(correlation_id);
            return;
        }
    };

    // 3a. Apply the pre-cancel maker partial fill to the *position* (only).
    //     The trade-row write is deferred to step 7 — we don't know yet
    //     whether the round-trip exit is "maker-only" (one atomic
    //     writer call) or "maker + FAK" (two writes), and we must avoid
    //     double-logging the same maker fill.
    if maker_filled > Decimal::ZERO {
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder {
            team, side, // revert side: SELL closes a BUY, BUY closes a SELL
            price: revert.revert_limit_price,
            size: maker_filled,
        });
        tracing::info!(
            order_id, label, %maker_filled, price = %revert.revert_limit_price,
            "timeout: applied pre-cancel maker partial fill to position",
        );
    }

    // 4. Compute exit size with full Decimal precision. None means already
    //    flat or residual smaller than `order_min_size` — write the
    //    maker-only round-trip and bail.
    let exit_size = match compute_taker_exit_size(
        revert.size, ws_filled_pre_cancel, rest_filled, config.order_min_size,
    ) {
        Some(s) => s,
        None => {
            tracing::info!(
                order_id, label,
                entry_size = %revert.size,
                maker_filled = %maker_filled,
                "timeout: nothing to exit (already flat or residual below min)",
            );
            app.push_event(
                "revert",
                &format!("{}: timeout — flat after partial fills, no taker exit needed", label),
            );

            // Choose the leg's terminal state. Three cases:
            //   - maker_filled == revert.size (full maker fill): Filled
            //     — economically the same as a normal maker exit.
            //   - 0 < maker_filled < revert.size (partial, residual
            //     below min): Partial — accurate, surfaces the open
            //     residual to the operator.
            //   - maker_filled == 0: Cancelled — nothing filled,
            //     residual was always sub-min (degenerate).
            let oid_for_state = order_id.to_string();
            let new_state = if maker_filled.is_zero() {
                crate::state::LegState::Cancelled {
                    order_id: oid_for_state,
                    reason: "timeout: residual below min size".into(),
                }
            } else if maker_filled >= revert.size {
                crate::state::LegState::Filled {
                    order_id: oid_for_state,
                    size: maker_filled,
                    avg_price: revert.revert_limit_price,
                    fee: Decimal::ZERO,
                }
            } else {
                crate::state::LegState::Partial {
                    order_id: oid_for_state,
                    filled: maker_filled,
                    requested: revert.size,
                    avg_price: revert.revert_limit_price,
                    fee: Decimal::ZERO,
                }
            };
            app.update_leg(correlation_id, leg_role, |leg| { leg.state = new_state; });

            // Maker-only round-trip writer: writes the trade row AND the
            // round_trip row atomically. No duplicate trade row because
            // step 3a only updated `position`, not the trade log.
            if maker_filled > Decimal::ZERO {
                let record = TradeRecord {
                    ts: crate::state::ist_now(),
                    side: format!("{}", side),
                    team: config.team_name(team).to_string(),
                    size: maker_filled,
                    price: revert.revert_limit_price,
                    cost: maker_filled * revert.revert_limit_price,
                    order_type: "GTC".into(),
                    label: label.to_string(),
                    order_id: order_id.to_string(),
                    fee: Decimal::ZERO,
                };
                log_revert_fill_with_round_trip(
                    app, config, &revert,
                    revert.revert_limit_price, maker_filled,
                    order_id, record,
                );
            }
            app.mark_group_closed_if_terminal(correlation_id);
            app.snapshot_inventory();
            return;
        }
    };

    // 4b. Maker + FAK path: log the maker trade row now (separate from the
    //     FAK trade row that the round-trip writer will produce). Two
    //     trade rows + one round_trip row total — economically correct.
    if maker_filled > Decimal::ZERO {
        app.log_trade(TradeRecord {
            ts: crate::state::ist_now(),
            side: format!("{}", side),
            team: config.team_name(team).to_string(),
            size: maker_filled,
            price: revert.revert_limit_price,
            cost: maker_filled * revert.revert_limit_price,
            order_type: "GTC".into(),
            label: format!("{label}_PRE_TIMEOUT_MAKER"),
            order_id: order_id.to_string(),
            fee: Decimal::ZERO,
        });
    }

    // 5. Read current book for L+1 pricing.
    let book = {
        let br = app.book_rx.read().unwrap();
        let books = match br.as_ref() {
            Some(rx) => rx.borrow().clone(),
            None => {
                tracing::error!(label, "timeout: no book available — taker exit aborted");
                app.update_leg(correlation_id, leg_role, |leg| {
                    leg.state = crate::state::LegState::Cancelled {
                        order_id: order_id.to_string(),
                        reason: "timeout: no book".into(),
                    };
                });
                app.mark_group_closed_if_terminal(correlation_id);
                return;
            }
        };
        match team {
            Team::TeamA => books.0,
            Team::TeamB => books.1,
        }
    };
    let exit_order = match build_taker_exit_fak(team, side, &book, exit_size) {
        Some(o) => o,
        None => {
            tracing::warn!(label, "timeout: opposite side empty — taker exit aborted");
            app.update_leg(correlation_id, leg_role, |leg| {
                leg.state = crate::state::LegState::Cancelled {
                    order_id: order_id.to_string(),
                    reason: "timeout: no liquidity on opposite side".into(),
                };
            });
            app.mark_group_closed_if_terminal(correlation_id);
            return;
        }
    };

    // 6. Submit the FAK. Dry-run path mirrors execute_event_trade's pattern.
    let exit_tag = format!("{label}_TIMEOUT_TAKER");
    if config.dry_run {
        tracing::info!(
            tag = %exit_tag, side = %exit_order.side, team = %config.team_name(team),
            price = %exit_order.price, size = %exit_order.size,
            "[DRY RUN] would fire taker exit FAK",
        );
        app.push_event(
            "trade",
            &format!("[DRY] {}: FAK exit {} @ {} sz={}", exit_tag, exit_order.side, exit_order.price, exit_order.size),
        );
        // Treat as fully filled at the limit price for state-tracking purposes.
        record_taker_exit_fill(
            config, app, position, &revert, &exit_order, exit_order.size,
            exit_order.price,
            maker_filled, revert.revert_limit_price,
            "dry_run_timeout", correlation_id, leg_role, label,
        );
        return;
    }

    let resp = match orders::post_fak_order(config, auth, &exit_order, &exit_tag).await {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(label, error = %e, "timeout: FAK submission failed");
            app.push_event("error", &format!("{}: timeout FAK failed: {}", label, e));
            // Persist the maker partial's round-trip row (trade row was
            // already written in step 4b). Without this the partial would
            // appear in `trades` but never in `round_trips`, hiding it
            // from net_pnl aggregates.
            write_round_trip_only_for_maker_partial(
                app, config, &revert, maker_filled, revert.revert_limit_price, order_id,
            );
            app.update_leg(correlation_id, leg_role, |leg| {
                leg.state = crate::state::LegState::Cancelled {
                    order_id: order_id.to_string(),
                    reason: format!("timeout FAK submit error: {e}"),
                };
            });
            app.mark_group_closed_if_terminal(correlation_id);
            return;
        }
    };
    let exit_oid = match resp.order_id.as_deref().filter(|s| !s.is_empty()) {
        Some(o) => o.to_string(),
        None => {
            let err = resp.error_msg.unwrap_or_default();
            tracing::warn!(label, err, "timeout: FAK rejected at submit");
            app.push_event("error", &format!("{}: timeout FAK rejected — {}", label, err));
            write_round_trip_only_for_maker_partial(
                app, config, &revert, maker_filled, revert.revert_limit_price, order_id,
            );
            app.update_leg(correlation_id, leg_role, |leg| {
                leg.state = crate::state::LegState::Cancelled {
                    order_id: order_id.to_string(),
                    reason: format!("timeout FAK rejected: {err}"),
                };
            });
            app.mark_group_closed_if_terminal(correlation_id);
            return;
        }
    };

    // 7. Detect the FAK exit fill. Race WS buffer + REST poll, same as entry.
    let poll_interval = Duration::from_millis(config.fill_poll_interval_ms.max(200));
    let poll_timeout = Duration::from_millis(config.fill_poll_timeout_ms);
    let fak_result = FakResult {
        order_id: Some(exit_oid.clone()),
        intended_order: exit_order.clone(),
        tag: exit_tag.clone(),
    };
    let signal_time = Instant::now(); // local timer; only affects latency record
    let fill = detect_fill(auth, app, Some(fak_result), poll_interval, poll_timeout, config, signal_time).await;

    let Some(fill) = fill else {
        tracing::error!(label, exit_oid, "timeout: FAK exit didn't fill — POSITION MAY BE STUCK");
        app.push_event("error", &format!("{}: timeout FAK never filled — manual flatten required", label));
        write_round_trip_only_for_maker_partial(
            app, config, &revert, maker_filled, revert.revert_limit_price, order_id,
        );
        app.update_leg(correlation_id, leg_role, |leg| {
            leg.state = crate::state::LegState::Cancelled {
                order_id: order_id.to_string(),
                reason: "timeout FAK exit didn't fill".into(),
            };
        });
        app.mark_group_closed_if_terminal(correlation_id);
        return;
    };

    record_taker_exit_fill(
        config, app, position, &revert, &exit_order,
        fill.filled_size, fill.avg_price,
        maker_filled, revert.revert_limit_price,
        &exit_oid, correlation_id, leg_role, label,
    );
}

/// Write a round_trip row for a maker partial fill when the subsequent FAK
/// taker exit failed (submit error, rejection, or never filled). The maker
/// trade row was already written via `app.log_trade` earlier in the flow,
/// so we must only persist the round_trip row here — `log_revert_fill_with_round_trip`
/// would re-write the trade row and double-count.
///
/// PnL is computed on the maker portion only (size = `maker_filled`) since
/// the residual (`revert.size − maker_filled`) is still open exposure that
/// the strategy could not auto-flatten — the operator must intervene
/// manually. `set_group_net_pnl` writes the partial net_pnl onto the
/// signal-group so the UI shows what *did* close.
fn write_round_trip_only_for_maker_partial(
    app: &Arc<AppState>,
    config: &Config,
    revert: &PendingRevert,
    maker_filled: Decimal,
    maker_avg_price: Decimal,
    exit_order_id: &str,
) {
    if maker_filled.is_zero() {
        return;
    }
    let pnl = match revert.side {
        Side::Sell => (maker_avg_price - revert.entry_price) * maker_filled,
        Side::Buy => (revert.entry_price - maker_avg_price) * maker_filled,
    };
    let fee_in = revert.entry_fee;
    let fee_out = Decimal::ZERO; // maker
    let net_pnl = pnl - fee_in - fee_out;

    let entry_side = match revert.side {
        Side::Sell => "BUY",
        Side::Buy => "SELL",
    };

    tracing::info!(
        team = %config.team_name(revert.team), entry_side,
        entry_price = %revert.entry_price, exit_price = %maker_avg_price,
        size = %maker_filled, pnl = %pnl.round_dp(4),
        fee_in = %fee_in.round_dp(4), fee_out = %fee_out.round_dp(4),
        net_pnl = %net_pnl.round_dp(4), label = %revert.label,
        "ROUND-TRIP partial (maker-only after FAK exit failure)",
    );

    let is_dry = exit_order_id.starts_with("dry_run");
    if !is_dry {
        if let Some(ref db) = *app.db.read().unwrap() {
            let slug = config.market_slug.clone();
            db.insert_round_trip(
                &revert.placed_at.elapsed().as_secs().to_string(),
                &crate::state::ist_now(),
                &config.team_name(revert.team),
                entry_side,
                &revert.entry_price.to_string(),
                &maker_avg_price.to_string(),
                &maker_filled.to_string(),
                &pnl.round_dp(6).to_string(),
                &revert.label,
                "",
                exit_order_id,
                &slug,
                &fee_in.round_dp(6).to_string(),
                &fee_out.round_dp(6).to_string(),
                &net_pnl.round_dp(6).to_string(),
            );
        }
    }
    app.set_group_net_pnl(&revert.correlation_id, net_pnl);
}

/// Apply a TakerExit fill and write a round-trip row that combines any
/// pre-cancel maker partial fill with the taker FAK fill.
///
/// PnL is computed against `revert.entry_price` over the **combined** exit
/// (`maker_filled + filled_size`), with `fee_out = exit_fee` (taker only;
/// maker fee is zero under `fd.to=true`). The maker partial's cash flow is
/// already applied to position+ledger in the caller, so this function only
/// applies the FAK fill.
#[allow(clippy::too_many_arguments)]
fn record_taker_exit_fill(
    config: &Config,
    app: &Arc<AppState>,
    position: &Position,
    revert: &PendingRevert,
    exit_order: &FakOrder,
    filled_size: Decimal,
    avg_price: Decimal,
    maker_filled: Decimal,
    maker_avg_price: Decimal,
    exit_order_id: &str,
    correlation_id: &str,
    leg_role: crate::state::LegRole,
    label: &str,
) {
    let cost = filled_size * avg_price;
    // Taker exit always pays the V2 fee, expressed in USDC.
    let fee_e = crate::fees::fee_exponent_as_u32(config.fee_exponent);
    let exit_fee = crate::fees::fee_usdc_sell(filled_size, avg_price, config.fee_rate, fee_e);

    {
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder {
            team: exit_order.team,
            side: exit_order.side,
            price: avg_price,
            size: filled_size,
        });
    }

    let msg = format!(
        "{label}: TAKER EXIT {} {} @ {} = ${} (fee ${})",
        exit_order.side, config.team_name(exit_order.team), avg_price,
        cost.round_dp(2), exit_fee.round_dp(4),
    );
    tracing::info!("{msg}");
    app.push_event("filled", &msg);

    // Round-trip exit metric: weighted average of the (optional) maker
    // partial and the taker FAK, applied to `revert.size` total.
    let combined_size = maker_filled + filled_size;
    let combined_avg_price = if combined_size.is_zero() {
        avg_price
    } else {
        (maker_filled * maker_avg_price + filled_size * avg_price) / combined_size
    };

    let trade_record = TradeRecord {
        ts: crate::state::ist_now(),
        side: format!("{}", exit_order.side),
        team: config.team_name(exit_order.team).to_string(),
        size: filled_size,
        price: avg_price,
        cost,
        order_type: "FAK".into(),
        label: format!("{label}_TIMEOUT_TAKER"),
        order_id: exit_order_id.to_string(),
        fee: exit_fee,
    };

    // Round-trip writer: pnl = (combined_avg − entry) · combined_size,
    // fee_in = revert.entry_fee, fee_out = exit_fee.
    log_revert_fill_with_round_trip(
        app, config, revert,
        combined_avg_price, combined_size,
        exit_order_id, trade_record,
    );

    // Flip leg state to terminal TakerExit and close the group.
    app.update_leg(correlation_id, leg_role, |leg| {
        leg.state = crate::state::LegState::TakerExit {
            order_id: exit_order_id.to_string(),
            size: filled_size,
            avg_price,
            fee: exit_fee,
        };
    });
    app.mark_group_closed_if_terminal(correlation_id);
    app.snapshot_inventory();
}

/// Build a sized FAK at **L0** (best touch) of the opposite side for the
/// taker-exit fallback.
///
/// `team` and `side` here are the side of the **exit** order (= the side of
/// the maker revert that just timed out). For a SELL-revert timeout we fire
/// a SELL FAK; for a BUY-revert timeout we fire a BUY FAK.
///
/// **Why L0 (not L+1 like entry FAKs):** Polymarket V2 BUY orders spend the
/// full `maker_amount` USDC budget — when matched at a better-than-limit
/// price the buyer receives MORE tokens, not less USDC paid. An L+1 limit
/// lets the matcher sweep both L0 and L+1; if L0 fills at a better price,
/// the spare USDC budget grabs additional tokens at L+1, *overshooting*
/// the requested `size`. On entry that's mostly OK (it reinforces the
/// directional bet), but on the timeout exit overshoot creates new
/// exposure in the OPPOSITE direction of what we're trying to flatten.
///
/// Using L0 caps the limit price at the touch — matcher can only fill at
/// L0 (or better, but better is also bounded by L0). If L0's depth is less
/// than `size`, FAK kills the remainder; we end with a small residual
/// position rather than an overshoot, which is the safer trade-off for a
/// flattening exit.
///
/// Size is the precomputed `remaining_size` from
/// [`compute_taker_exit_size`] — never re-derived from `max_trade_usdc`,
/// because the goal here is *flatten the position*, not size by budget.
pub(crate) fn build_taker_exit_fak(
    team: Team,
    side: Side,
    book: &OrderBook,
    size: Decimal,
) -> Option<FakOrder> {
    let levels = match side {
        Side::Buy => &book.asks.levels,
        Side::Sell => &book.bids.levels,
    };
    if levels.is_empty() || size.is_zero() {
        return None;
    }
    // L0 only — the touch price. No L+1 sweep, no overshoot.
    let price = levels[0].price;
    Some(FakOrder { team, side, price, size })
}

/// Compute the size for the taker-exit FAK fired when a revert GTC times out.
///
/// ```text
///   filled    = max(cancel_size_matched, get_order_size_matched)  # race-safe
///   exit_size = max(0, entry_fill_size − filled)
///   None  if exit_size < order_min_size  # residual too small to FAK
/// ```
///
/// All math is `Decimal` — no `f64`. The `max()` of the two `filled`
/// observations handles the race where a partial fill landed between the
/// cancel HTTP and the subsequent `get_order` HTTP: the larger value is
/// authoritative because fills only ever increase the size_matched counter.
///
/// Returns `None` if there is nothing meaningful to exit (already flat, or
/// residual smaller than the venue's minimum order size).
pub(crate) fn compute_taker_exit_size(
    entry_fill_size: Decimal,
    cancel_size_matched: Decimal,
    get_order_size_matched: Decimal,
    order_min_size: Decimal,
) -> Option<Decimal> {
    let filled = cancel_size_matched.max(get_order_size_matched);
    let remaining = (entry_fill_size - filled).max(Decimal::ZERO);
    if remaining < order_min_size || remaining.is_zero() {
        return None;
    }
    Some(remaining)
}

/// Human-readable label for the per-signal panel.
fn signal_label_for_panel(signal: &CricketSignal) -> &'static str {
    match signal {
        CricketSignal::Wicket(_) => "WICKET",
        CricketSignal::Runs(_) => "RUN",
        CricketSignal::Wide(_) => "WD",
        CricketSignal::NoBall(_) => "NB",
        CricketSignal::InningsOver => "IO",
        CricketSignal::MatchOver => "MO",
    }
}

/// V2 fee in USDC for a revert (GTC) fill. With `fd.to=true` (the production
/// case, enforced at startup in `server.rs`) makers pay zero. The function
/// still computes the fee when `takers_only_fees=false` so a future flip in
/// market config doesn't silently mis-attribute fees.
pub(crate) fn revert_exit_fee(config: &Config, filled: Decimal, price: Decimal) -> Decimal {
    if config.takers_only_fees || config.fee_rate == 0.0 || filled.is_zero() {
        return Decimal::ZERO;
    }
    let e = crate::fees::fee_exponent_as_u32(config.fee_exponent);
    crate::fees::fee_usdc_sell(filled, price, config.fee_rate, e)
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
/// Decimal places implied by `tick`, capped at 6 to defend against an f64
/// round-trip that smuggled noise digits into the cached tick (e.g.,
/// `0.01` arriving as `0.0100000000000000002081668171`). Without the cap,
/// `round_dp(28)` would preserve the noise, producing limit prices like
/// `0.21000000000000000043715031591` everywhere.
fn tick_decimal_places(tick: Decimal) -> u32 {
    let raw = tick.to_string()
        .split('.')
        .nth(1)
        .map_or(0, |frac| frac.trim_end_matches('0').len());
    raw.min(6) as u32
}

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
///
/// Reverts (`purpose == "REVERT_GTC"`) are posted with `postOnly=true` so the
/// matcher rejects rather than crosses. On a cross-reject we **immediately
/// retry without postOnly** (no sleep) so the crossing portion captures
/// the now-better market price as a taker fill while the residual rests as
/// a passive maker. The 15s `revert_timeout_ms` window then applies to
/// whatever rests after that retry.
async fn execute_limit(
    config: &Config, auth: &ClobAuth, order: &FakOrder,
    _position: &Position, tag: &str, app: &Arc<AppState>,
    correlation_id: &str,
    purpose: &str,
    replaces_order_id: Option<&str>,
) -> Option<String> {
    if config.dry_run {
        let notional = order.price * order.size;
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, notional = %notional,
            "[DRY RUN] would place GTC limit order");
        app.push_event("trade", &format!("[DRY] {tag}: GTC {} {} @ {} sz={}", order.side, config.team_name(order.team), order.price, order.size));
        return Some(format!("dry_run_{tag}"));
    }

    // Post-only is enabled by default for revert reposts so we never
    // accidentally pay taker fee on what was meant to be a maker. Other
    // purposes (e.g. maker-engine quotes that already manage their own
    // post-only) keep the previous behaviour.
    let post_only = purpose == "REVERT_GTC";

    let resp = match orders::post_limit_order_with_post_only(config, auth, order, tag, post_only).await {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(tag, error = %e, "GTC limit order failed");
            app.push_event("error", &format!("{tag}: {e}"));
            return None;
        }
    };

    // Cross-reject retry path: book moved further in our favour during the
    // ~3s sports delay, so the maker price is now on the wrong side of the
    // touch. Retry plain GTC immediately — the crossing portion fills as
    // taker at the now-better market price, residual rests as maker.
    let resp = if resp.order_id.is_none() && post_only {
        let err = resp.error_msg.as_deref().unwrap_or("");
        if orders::is_post_only_cross_reject(err) {
            tracing::warn!(tag, err, "post-only would cross — retrying plain GTC immediately");
            app.push_event("warn", &format!("{tag}: post-only would cross — retrying plain GTC"));
            match orders::post_limit_order_with_post_only(config, auth, order, tag, false).await {
                Ok(r) => r,
                Err(e) => {
                    tracing::error!(tag, error = %e, "plain GTC retry failed");
                    app.push_event("error", &format!("{tag}: plain retry: {e}"));
                    return None;
                }
            }
        } else {
            resp
        }
    } else {
        resp
    };

    if let Some(oid) = resp.order_id {
        let cost = order.price * order.size;
        tracing::info!(tag, order_id = %oid, "GTC limit order placed");
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
            // Placement-time stub — no fill yet; fee is set when the
            // matcher reports a fill against this order.
            fee: Decimal::ZERO,
        });

        // B5: persist placement metadata. AUGMENTs pass `replaces_order_id`
        // so the chain back to the original revert is queryable.
        if let Some(ref db) = *app.db.read().unwrap() {
            let asset_id = config.token_id(order.team).to_string();
            let side_str = format!("{}", order.side);
            let price_str = order.price.to_string();
            let size_str = order.size.to_string();
            let team_str = config.team_name(order.team).to_string();
            let ts = crate::state::ist_now();
            db.record_order_placement(&crate::db::OrderPlacement {
                order_id: &oid,
                slug: &config.market_slug,
                asset_id: &asset_id,
                side: &side_str,
                price: &price_str,
                original_size: &size_str,
                status: "live",
                order_type: "GTC",
                created_at: &ts,
                team: &team_str,
                correlation_id,
                purpose,
                replaces_order_id,
            });
        }

        Some(oid)
    } else {
        let msg = resp.error_msg.unwrap_or_default();
        app.push_event("error", &format!("{tag}: GTC rejected — {msg}"));
        None
    }
}
