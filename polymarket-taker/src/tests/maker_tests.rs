use rust_decimal_macros::dec;

use crate::maker::{
    cancellation_legs, compute_fair_value, compute_quote_prices, compute_reservation_price,
    inventory_tier, InventoryTier, QuoteLeg,
};
use crate::config::MakerConfig;
use crate::types::{
    CricketSignal, OrderBook, OrderBookSide, PriceLevel, Team,
};

// ── compute_fair_value ───────────────────────────────────────────────────────

#[test]
fn test_compute_fair_value() {
    let book = OrderBook {
        bids: OrderBookSide {
            levels: vec![PriceLevel { price: dec!(0.50), size: dec!(100) }],
        },
        asks: OrderBookSide {
            levels: vec![PriceLevel { price: dec!(0.52), size: dec!(100) }],
        },
        timestamp_ms: 0,
    };
    let fair = compute_fair_value(&book).unwrap();
    assert_eq!(fair, dec!(0.51));
}

#[test]
fn test_compute_fair_value_no_book() {
    let book = OrderBook::default();
    assert!(compute_fair_value(&book).is_none());

    // Only bids, no asks
    let book2 = OrderBook {
        bids: OrderBookSide {
            levels: vec![PriceLevel { price: dec!(0.50), size: dec!(100) }],
        },
        asks: OrderBookSide { levels: vec![] },
        timestamp_ms: 0,
    };
    assert!(compute_fair_value(&book2).is_none());
}

// ── cancellation_matrix ──────────────────────────────────────────────────────

#[test]
fn test_cancellation_matrix_wicket() {
    // TeamA batting: wicket should cancel batting-BUY (A-BUY) + bowling-SELL (B-SELL)
    let legs = cancellation_legs(&CricketSignal::Wicket(0), Team::TeamA);
    assert_eq!(legs.len(), 2);
    assert!(legs.contains(&QuoteLeg::TeamABuy));
    assert!(legs.contains(&QuoteLeg::TeamBSell));
}

#[test]
fn test_cancellation_matrix_boundary() {
    // TeamA batting, 6 runs: cancel batting-SELL (A-SELL) + bowling-BUY (B-BUY)
    let legs = cancellation_legs(&CricketSignal::Runs(6), Team::TeamA);
    assert_eq!(legs.len(), 2);
    assert!(legs.contains(&QuoteLeg::TeamASell));
    assert!(legs.contains(&QuoteLeg::TeamBBuy));

    // Same for 4 runs
    let legs4 = cancellation_legs(&CricketSignal::Runs(4), Team::TeamA);
    assert_eq!(legs4.len(), 2);
    assert!(legs4.contains(&QuoteLeg::TeamASell));
    assert!(legs4.contains(&QuoteLeg::TeamBBuy));
}

#[test]
fn test_cancellation_matrix_dot() {
    let legs = cancellation_legs(&CricketSignal::Runs(0), Team::TeamA);
    assert!(legs.is_empty());

    let legs1 = cancellation_legs(&CricketSignal::Runs(1), Team::TeamB);
    assert!(legs1.is_empty());

    let legs3 = cancellation_legs(&CricketSignal::Runs(3), Team::TeamA);
    assert!(legs3.is_empty());
}

#[test]
fn test_cancellation_matrix_innings_over() {
    let legs = cancellation_legs(&CricketSignal::InningsOver, Team::TeamA);
    assert_eq!(legs.len(), 4);
    // Should contain all 4 legs
    assert!(legs.contains(&QuoteLeg::TeamABuy));
    assert!(legs.contains(&QuoteLeg::TeamASell));
    assert!(legs.contains(&QuoteLeg::TeamBBuy));
    assert!(legs.contains(&QuoteLeg::TeamBSell));
}

// ── inventory_tier ───────────────────────────────────────────────────────────

#[test]
fn test_inventory_tier_green() {
    let tier = inventory_tier(dec!(10), dec!(100), 0.20, 0.50, 0.80);
    assert_eq!(tier, InventoryTier::Green);
}

#[test]
fn test_inventory_tier_yellow() {
    let tier = inventory_tier(dec!(25), dec!(100), 0.20, 0.50, 0.80);
    assert_eq!(tier, InventoryTier::Yellow);
}

#[test]
fn test_inventory_tier_orange() {
    let tier = inventory_tier(dec!(60), dec!(100), 0.20, 0.50, 0.80);
    assert_eq!(tier, InventoryTier::Orange);
}

#[test]
fn test_inventory_tier_red() {
    let tier = inventory_tier(dec!(85), dec!(100), 0.20, 0.50, 0.80);
    assert_eq!(tier, InventoryTier::Red);
}

// ── skew computation ─────────────────────────────────────────────────────────

#[test]
fn test_skew_computation() {
    let fair = dec!(0.50);
    let kappa = dec!(0.001);

    // No exposure => reservation == fair
    let r0 = compute_reservation_price(fair, dec!(0), kappa);
    assert_eq!(r0, dec!(0.50));

    // Long exposure => reservation shifts down (willing to buy lower)
    let r_long = compute_reservation_price(fair, dec!(100), kappa);
    assert!(r_long < fair);
    assert_eq!(r_long, dec!(0.40));

    // Short exposure => reservation shifts up
    let r_short = compute_reservation_price(fair, dec!(-100), kappa);
    assert!(r_short > fair);
    assert_eq!(r_short, dec!(0.60));
}

// ── quote prices ─────────────────────────────────────────────────────────────

#[test]
fn test_quote_price_computation() {
    let reservation = dec!(0.50);
    let half_spread = dec!(0.02);
    let (bid, ask) = compute_quote_prices(reservation, half_spread, "0.01");
    assert_eq!(bid, dec!(0.48));
    assert_eq!(ask, dec!(0.52));
}

// ── complementary pricing ────────────────────────────────────────────────────

#[test]
fn test_complementary_pricing() {
    let fair_a = dec!(0.60);
    let fair_b = rust_decimal::Decimal::ONE - fair_a;
    assert_eq!(fair_b, dec!(0.40));

    let fair_a2 = dec!(0.73);
    let fair_b2 = rust_decimal::Decimal::ONE - fair_a2;
    assert_eq!(fair_b2, dec!(0.27));
}

// ── dry_run default ──────────────────────────────────────────────────────────

#[test]
fn test_dry_run_flag() {
    let cfg = MakerConfig::default();
    assert!(cfg.dry_run, "MakerConfig must default to dry_run=true");
    assert!(!cfg.enabled, "MakerConfig must default to enabled=false");
}
