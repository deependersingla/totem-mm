use rust_decimal::Decimal;
use std::sync::{Arc, Mutex};

use crate::config::Config;
use crate::types::{FakOrder, Side, Team};

#[derive(Debug, Clone)]
pub struct PositionInner {
    pub team_a_tokens: Decimal,
    pub team_b_tokens: Decimal,
    pub total_spent: Decimal,
    pub trade_count: u64,
    pub total_budget: Decimal,
}

impl PositionInner {
    pub fn remaining_budget(&self) -> Decimal {
        (self.total_budget - self.total_spent).max(Decimal::ZERO)
    }

    pub fn can_spend(&self, amount: Decimal) -> bool {
        self.total_spent + amount <= self.total_budget
    }

    pub fn on_fill(&mut self, order: &FakOrder) {
        let notional = order.price * order.size;
        let tokens = match order.team {
            Team::TeamA => &mut self.team_a_tokens,
            Team::TeamB => &mut self.team_b_tokens,
        };

        match order.side {
            Side::Buy => {
                *tokens += order.size;
                self.total_spent += notional;
            }
            Side::Sell => {
                *tokens -= order.size;
                // selling recovers cash — don't add to spent
            }
        }

        self.trade_count += 1;
    }

    pub fn summary(&self, config: &Config) -> String {
        format!(
            "{}={} {}={} spent={}/{} remaining={} trades={}",
            config.team_a_name, self.team_a_tokens,
            config.team_b_name, self.team_b_tokens,
            self.total_spent, self.total_budget,
            self.remaining_budget(),
            self.trade_count
        )
    }
}

/// Thread-safe position tracker shared between strategy main loop and spawned revert tasks
pub type Position = Arc<Mutex<PositionInner>>;

pub fn new_position(total_budget: Decimal) -> Position {
    Arc::new(Mutex::new(PositionInner {
        team_a_tokens: Decimal::ZERO,
        team_b_tokens: Decimal::ZERO,
        total_spent: Decimal::ZERO,
        trade_count: 0,
        total_budget,
    }))
}
