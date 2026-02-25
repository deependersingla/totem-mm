use std::sync::Arc;

use rust_decimal::Decimal;
use tokio::sync::{mpsc, watch};

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::orders;
use crate::position::Position;
use crate::state::AppState;
use crate::types::{CricketSignal, FakOrder, MatchState, OrderBook, Side, Team};

pub async fn run(
    config: &Config,
    auth: &ClobAuth,
    mut signal_rx: mpsc::Receiver<CricketSignal>,
    book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    position: Position,
    app: Arc<AppState>,
) {
    let mut state = MatchState::new(config.first_batting);

    tracing::info!(
        batting = %config.team_name(state.batting),
        bowling = %config.team_name(state.bowling()),
        innings = state.innings,
        dry_run = config.dry_run,
        "strategy engine started"
    );

    while let Some(signal) = signal_rx.recv().await {
        let config = app.config.read().unwrap().clone();

        match signal {
            CricketSignal::MatchOver => {
                tracing::info!("MO received — shutting down strategy");
                let pos = position.lock().unwrap();
                tracing::info!(position = %pos.summary(&config), "final position");
                app.push_event("strategy", "match over — strategy stopped");
                break;
            }

            CricketSignal::InningsOver => {
                state.switch_innings();
                *app.match_state.write().unwrap() = state.clone();
                let msg = format!("innings over — {} now batting (innings {})",
                    config.team_name(state.batting), state.innings);
                tracing::info!("{msg}");
                app.push_event("innings", &msg);
            }

            CricketSignal::Wicket => {
                let batting = state.batting;
                let bowling = state.bowling();

                let msg = format!("WICKET — sell {} buy {}", config.team_name(batting), config.team_name(bowling));
                tracing::info!("{msg}");
                app.push_event("wicket", &msg);

                let books = book_rx.borrow().clone();
                let (batting_book, bowling_book) = team_books(&books, batting);

                let sell_order = build_sell_order(&config, batting, &batting_book);
                let buy_order = build_buy_order(&config, bowling, &bowling_book);

                let mut sell_entry_price = None;
                let mut buy_entry_price = None;

                if let Some(ref order) = sell_order {
                    sell_entry_price = Some(order.price);
                    execute_fak(&config, auth, order, &position, "WICKET_SELL", &app).await;
                }
                if let Some(ref order) = buy_order {
                    buy_entry_price = Some(order.price);
                    execute_fak(&config, auth, order, &position, "WICKET_BUY", &app).await;
                }

                let delay = config.revert_delay_ms;
                let revert_config = config.clone();
                let revert_auth = auth.clone();
                let revert_position = position.clone();
                let revert_app = app.clone();

                tokio::spawn(async move {
                    tokio::time::sleep(std::time::Duration::from_millis(delay)).await;

                    tracing::info!(delay_ms = delay, "REVERT — placing limit orders at entry prices");
                    revert_app.push_event("revert", &format!("placing revert orders after {delay}ms"));

                    if let Some(entry) = sell_entry_price {
                        if let Some(ref sell_order) = sell_order {
                            let revert = FakOrder {
                                team: batting,
                                side: Side::Buy,
                                price: entry,
                                size: sell_order.size,
                            };
                            execute_limit(&revert_config, &revert_auth, &revert, &revert_position, "REVERT_BUY", &revert_app).await;
                        }
                    }

                    if let Some(entry) = buy_entry_price {
                        if let Some(ref buy_order) = buy_order {
                            let revert = FakOrder {
                                team: bowling,
                                side: Side::Sell,
                                price: entry,
                                size: buy_order.size,
                            };
                            execute_limit(&revert_config, &revert_auth, &revert, &revert_position, "REVERT_SELL", &revert_app).await;
                        }
                    }
                });
            }

            CricketSignal::Runs(r) => {
                tracing::debug!(runs = r, batting = %config.team_name(state.batting), "runs scored");
                app.push_event("ball", &format!("{r} runs"));
            }
            CricketSignal::Wide(r) => {
                tracing::debug!(extra_runs = r, "wide");
                app.push_event("ball", &format!("wide +{r}"));
            }
            CricketSignal::NoBall => {
                tracing::debug!("no ball");
                app.push_event("ball", "no ball");
            }
        }
    }

    tracing::info!("strategy engine stopped");
}

pub async fn buy_initial_tokens(
    config: &Config,
    auth: &ClobAuth,
    book_rx: &watch::Receiver<(OrderBook, OrderBook)>,
    position: &Position,
    app: &Arc<AppState>,
) {
    if config.initial_buy_usdc <= Decimal::ZERO {
        tracing::info!("initial_buy_usdc=0, skipping initial token purchase");
        return;
    }

    let per_team = config.initial_buy_usdc / Decimal::TWO;
    tracing::info!(per_team = %per_team, "buying initial tokens for both teams");

    let books = book_rx.borrow().clone();

    if let Some(ask) = books.0.best_ask() {
        let size = (per_team / ask.price).min(ask.size);
        if size > Decimal::ZERO {
            let order = FakOrder { team: Team::TeamA, side: Side::Buy, price: ask.price, size };
            execute_fak(config, auth, &order, position, "INITIAL_BUY_A", app).await;
        }
    } else {
        tracing::warn!("no ask for team A — can't buy initial tokens");
        app.push_event("warn", "no ask for team A");
    }

    if let Some(ask) = books.1.best_ask() {
        let size = (per_team / ask.price).min(ask.size);
        if size > Decimal::ZERO {
            let order = FakOrder { team: Team::TeamB, side: Side::Buy, price: ask.price, size };
            execute_fak(config, auth, &order, position, "INITIAL_BUY_B", app).await;
        }
    } else {
        tracing::warn!("no ask for team B — can't buy initial tokens");
        app.push_event("warn", "no ask for team B");
    }

    let pos = position.lock().unwrap();
    let summary = pos.summary(config);
    tracing::info!(position = %summary, "initial tokens purchased");
    app.push_event("trade", &format!("initial buy done: {summary}"));
}

fn team_books(books: &(OrderBook, OrderBook), team: Team) -> (OrderBook, OrderBook) {
    match team {
        Team::TeamA => (books.0.clone(), books.1.clone()),
        Team::TeamB => (books.1.clone(), books.0.clone()),
    }
}

fn build_sell_order(config: &Config, team: Team, book: &OrderBook) -> Option<FakOrder> {
    let best_bid = book.best_bid()?;
    let size = compute_size(config, &best_bid.size, best_bid.price);
    if size.is_zero() {
        tracing::warn!(team = %config.team_name(team), "no bid liquidity to sell into");
        return None;
    }
    Some(FakOrder { team, side: Side::Sell, price: best_bid.price, size })
}

fn build_buy_order(config: &Config, team: Team, book: &OrderBook) -> Option<FakOrder> {
    let best_ask = book.best_ask()?;
    let size = compute_size(config, &best_ask.size, best_ask.price);
    if size.is_zero() {
        tracing::warn!(team = %config.team_name(team), "no ask liquidity to buy from");
        return None;
    }
    Some(FakOrder { team, side: Side::Buy, price: best_ask.price, size })
}

fn compute_size(config: &Config, available: &Decimal, price: Decimal) -> Decimal {
    if price.is_zero() { return Decimal::ZERO; }
    let max_tokens = config.max_trade_usdc / price;
    max_tokens.min(*available)
}

async fn execute_fak(
    config: &Config, auth: &ClobAuth, order: &FakOrder,
    position: &Position, tag: &str, app: &Arc<AppState>,
) {
    let notional = order.price * order.size;

    {
        let pos = position.lock().unwrap();
        if order.side == Side::Buy && !pos.can_spend(notional) {
            tracing::warn!(tag, notional = %notional, remaining = %pos.remaining_budget(), "budget exceeded — skipping");
            app.push_event("warn", &format!("{tag}: budget exceeded, skipping"));
            return;
        }
    }

    if config.dry_run {
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, notional = %notional,
            "[DRY RUN] would place FOK order");
        position.lock().unwrap().on_fill(order);
        app.push_event("trade", &format!("[DRY] {tag}: {} {} @ {} sz={}", order.side, config.team_name(order.team), order.price, order.size));
        return;
    }

    match orders::post_fak_order(config, auth, order, tag).await {
        Ok(resp) if resp.order_id.is_some() => {
            position.lock().unwrap().on_fill(order);
            let oid = resp.order_id.unwrap();
            app.push_event("trade", &format!("{tag}: {} {} @ {} sz={} ({})", order.side, config.team_name(order.team), order.price, order.size, oid));
        }
        Ok(resp) => {
            let msg = resp.error_msg.unwrap_or_default();
            app.push_event("error", &format!("{tag}: rejected — {msg}"));
        }
        Err(e) => {
            tracing::error!(tag, error = %e, "FOK order failed");
            app.push_event("error", &format!("{tag}: {e}"));
        }
    }
}

async fn execute_limit(
    config: &Config, auth: &ClobAuth, order: &FakOrder,
    _position: &Position, tag: &str, app: &Arc<AppState>,
) {
    if config.dry_run {
        let notional = order.price * order.size;
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, notional = %notional,
            "[DRY RUN] would place GTC limit order");
        app.push_event("trade", &format!("[DRY] {tag}: GTC {} {} @ {} sz={}", order.side, config.team_name(order.team), order.price, order.size));
        return;
    }

    match orders::post_limit_order(config, auth, order, tag).await {
        Ok(resp) if resp.order_id.is_some() => {
            let oid = resp.order_id.unwrap();
            tracing::info!(tag, order_id = oid, "GTC limit order placed");
            app.track_order(oid.clone());
            app.push_event("trade", &format!("{tag}: GTC {} {} @ {} ({})", order.side, config.team_name(order.team), order.price, oid));
        }
        Ok(resp) => {
            let msg = resp.error_msg.unwrap_or_default();
            app.push_event("error", &format!("{tag}: GTC rejected — {msg}"));
        }
        Err(e) => {
            tracing::error!(tag, error = %e, "GTC limit order failed");
            app.push_event("error", &format!("{tag}: {e}"));
        }
    }
}
