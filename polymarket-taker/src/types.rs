use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Which team's token on Polymarket
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Team {
    TeamA,
    TeamB,
}

impl Team {
    pub fn opponent(self) -> Self {
        match self {
            Team::TeamA => Team::TeamB,
            Team::TeamB => Team::TeamA,
        }
    }
}

impl std::fmt::Display for Team {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Team::TeamA => write!(f, "TEAM_A"),
            Team::TeamB => write!(f, "TEAM_B"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Side {
    Buy,
    Sell,
}

impl std::fmt::Display for Side {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Side::Buy => write!(f, "BUY"),
            Side::Sell => write!(f, "SELL"),
        }
    }
}

/// Raw cricket delivery signal from the oracle / telegram bot
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CricketSignal {
    Runs(u8),         // 0..6
    Wicket(u8),       // W (0 extra runs), W1..W6 (runs scored on wicket ball)
    Wide(u8),         // Wd0..Wd6
    NoBall(u8),       // N0..N6
    InningsOver,      // IO — batting team switches
    MatchOver,        // MO — stop everything
}

impl CricketSignal {
    pub fn is_wicket(&self) -> bool {
        matches!(self, Self::Wicket(_))
    }

    /// Parse a raw string into a cricket signal.
    /// Formats: "0".."6", "W","W0".."W6", "Wd","Wd0".."Wd6", "N","N0".."N6", "IO", "MO"
    pub fn parse(raw: &str) -> Option<Self> {
        let s = raw.trim();
        match s {
            "IO" => Some(Self::InningsOver),
            "MO" => Some(Self::MatchOver),
            "W" => Some(Self::Wicket(0)),
            "N" => Some(Self::NoBall(0)),
            "Wd" => Some(Self::Wide(0)),
            _ if s.starts_with("Wd") => {
                let runs: u8 = s.strip_prefix("Wd")?.parse().ok()?;
                if runs <= 6 { Some(Self::Wide(runs)) } else { None }
            }
            _ if s.starts_with('W') => {
                let runs: u8 = s.strip_prefix('W')?.parse().ok()?;
                if runs <= 6 { Some(Self::Wicket(runs)) } else { None }
            }
            _ if s.starts_with('N') => {
                let runs: u8 = s.strip_prefix('N')?.parse().ok()?;
                if runs <= 6 { Some(Self::NoBall(runs)) } else { None }
            }
            _ => {
                let runs: u8 = s.parse().ok()?;
                if runs <= 6 { Some(Self::Runs(runs)) } else { None }
            }
        }
    }
}

impl std::fmt::Display for CricketSignal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Runs(r) => write!(f, "{r}"),
            Self::Wicket(0) => write!(f, "W"),
            Self::Wicket(r) => write!(f, "W{r}"),
            Self::Wide(0) => write!(f, "Wd"),
            Self::Wide(r) => write!(f, "Wd{r}"),
            Self::NoBall(0) => write!(f, "N"),
            Self::NoBall(r) => write!(f, "N{r}"),
            Self::InningsOver => write!(f, "IO"),
            Self::MatchOver => write!(f, "MO"),
        }
    }
}

/// Tracks which team is currently batting
#[derive(Debug, Clone)]
pub struct MatchState {
    pub batting: Team,
    pub innings: u8,
}

impl MatchState {
    pub fn new(first_batting: Team) -> Self {
        Self {
            batting: first_batting,
            innings: 1,
        }
    }

    pub fn bowling(&self) -> Team {
        self.batting.opponent()
    }

    pub fn switch_innings(&mut self) {
        self.batting = self.batting.opponent();
        self.innings += 1;
    }
}

/// An order we want to place on the CLOB
#[derive(Debug, Clone)]
pub struct FakOrder {
    pub team: Team,
    pub side: Side,
    pub price: Decimal,
    pub size: Decimal,
}

impl std::fmt::Display for FakOrder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {} @ {} sz={}", self.side, self.team, self.price, self.size)
    }
}

#[derive(Debug, Clone)]
pub struct PriceLevel {
    pub price: Decimal,
    pub size: Decimal,
}

#[derive(Debug, Clone, Default)]
pub struct OrderBookSide {
    pub levels: Vec<PriceLevel>,
}

impl OrderBookSide {
    pub fn best(&self) -> Option<&PriceLevel> {
        self.levels.first()
    }
}

#[derive(Debug, Clone, Default)]
pub struct OrderBook {
    pub bids: OrderBookSide,
    pub asks: OrderBookSide,
    pub timestamp_ms: u64,
}

impl OrderBook {
    pub fn best_bid(&self) -> Option<&PriceLevel> {
        self.bids.best()
    }

    pub fn best_ask(&self) -> Option<&PriceLevel> {
        self.asks.best()
    }
}
