//! Duckworth-Lewis-Stern (Standard Edition) for T20 cricket.
//!
//! Computes par scores for a chasing side at any state of the innings using
//! the publicly-published ICC Standard Edition resource table. For IPL the
//! umpires apply the Professional Edition (proprietary); for typical
//! scoring ranges the Standard Edition is within ~1-2 runs and is sufficient
//! as a trading signal.
//!
//! ## Core model
//!
//! A T20 innings of 20 overs + 10 wickets in hand = 100% of scoring
//! resources. As balls are consumed and wickets fall, resources deplete
//! along a non-linear curve. Given Team 1's total and Team 2's remaining
//! resources at any point, par = T1_total × resources_used / resources_total.
//!
//! ## T20 minimum overs
//!
//! A DLS result is only valid in the chase once **5 overs (30 balls)** have
//! been bowled in the second innings. Below that, an abandoned match is
//! declared "no result". [`DlsEngine::is_result_valid`] exposes this.

use crate::types::CricketSignal;

// ── Resource table ────────────────────────────────────────────────────────

/// T20 DLS Standard Edition resource table.
///
/// Indexing: `T20_TABLE[balls_remaining / 6][wickets_lost]`.
/// Rows step in 6-ball (1-over) increments from 0 to 120.
/// Columns span wickets lost 0..=9.
/// Values are percentage of full T20 innings resources remaining.
///
/// Published by the ICC in their DLS Standard Edition methodology document.
const T20_TABLE: [[f64; 10]; 21] = [
    // balls_remaining = 0
    [ 0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.0,  0.0, 0.0],
    // balls_remaining = 6 (19 overs used, 1 left)
    [ 6.4,  6.4,  6.4,  6.4,  6.4,  6.2,  6.2,  6.0,  5.7, 4.4],
    // balls_remaining = 12
    [12.7, 12.5, 12.5, 12.4, 12.4, 12.0, 11.7, 11.0,  9.7, 6.5],
    // balls_remaining = 18
    [18.7, 18.6, 18.4, 18.2, 18.0, 17.5, 16.8, 15.4, 12.7, 7.4],
    // balls_remaining = 24
    [24.6, 24.4, 24.2, 23.9, 23.3, 22.4, 21.2, 18.9, 14.8, 8.0],
    // balls_remaining = 30 (15 overs used)
    [30.4, 30.0, 29.7, 29.2, 28.4, 27.2, 25.3, 22.1, 16.6, 8.1],
    // balls_remaining = 36
    [35.9, 35.5, 35.0, 34.3, 33.2, 31.4, 29.0, 24.6, 17.8, 8.1],
    // balls_remaining = 42
    [41.3, 40.8, 40.1, 39.2, 37.8, 35.5, 32.2, 26.9, 18.6, 8.3],
    // balls_remaining = 48
    [46.6, 45.9, 45.1, 43.8, 42.0, 39.4, 35.2, 28.6, 19.3, 8.3],
    // balls_remaining = 54
    [51.8, 51.1, 49.8, 48.4, 46.1, 42.8, 37.8, 30.2, 19.8, 8.3],
    // balls_remaining = 60 (10 overs used)
    [56.7, 55.8, 54.4, 52.7, 50.0, 46.1, 40.3, 31.6, 20.1, 8.3],
    // balls_remaining = 66
    [61.7, 60.4, 59.0, 56.7, 53.7, 49.1, 42.4, 32.7, 20.3, 8.3],
    // balls_remaining = 72
    [66.4, 65.0, 63.3, 60.6, 57.1, 51.9, 44.3, 33.6, 20.5, 8.3],
    // balls_remaining = 78
    [71.0, 69.4, 67.3, 64.5, 60.4, 54.4, 46.1, 34.5, 20.7, 8.3],
    // balls_remaining = 84
    [75.4, 73.7, 71.4, 68.0, 63.4, 56.9, 47.7, 35.2, 20.8, 8.3],
    // balls_remaining = 90
    [79.9, 77.9, 75.3, 71.6, 66.4, 59.2, 49.1, 35.7, 20.8, 8.3],
    // balls_remaining = 96
    [84.1, 81.8, 79.0, 74.7, 69.1, 61.3, 50.4, 36.2, 20.8, 8.3],
    // balls_remaining = 102
    [88.2, 85.7, 82.5, 77.9, 71.7, 63.3, 51.6, 36.6, 21.0, 8.3],
    // balls_remaining = 108
    [92.2, 89.6, 85.9, 81.1, 74.2, 65.0, 52.7, 36.9, 21.0, 8.3],
    // balls_remaining = 114
    [96.1, 93.3, 89.2, 83.9, 76.7, 66.6, 53.5, 37.3, 21.0, 8.3],
    // balls_remaining = 120 (full innings, 20 overs)
    [100.0, 96.8, 92.6, 86.7, 78.8, 68.2, 54.4, 37.5, 21.3, 8.3],
];

const T20_MAX_BALLS: u16 = 120;
const T20_MIN_CHASE_BALLS_FOR_RESULT: u16 = 30;

// ── Resource lookup ───────────────────────────────────────────────────────

/// Remaining resource percentage for a T20 innings at `balls_remaining`
/// with `wickets_lost` wickets already fallen.
///
/// For non-integer over counts (ball-level precision), linearly interpolates
/// between the two adjacent 6-ball rows.
pub fn resource_t20(balls_remaining: u16, wickets_lost: u8) -> f64 {
    let balls = balls_remaining.min(T20_MAX_BALLS);
    let wkts = wickets_lost.min(9) as usize;

    let lower_idx = (balls / 6) as usize;
    let rem = (balls % 6) as f64;
    let lower = T20_TABLE[lower_idx][wkts];

    if rem == 0.0 || lower_idx + 1 >= T20_TABLE.len() {
        return lower;
    }

    let upper = T20_TABLE[lower_idx + 1][wkts];
    lower + (upper - lower) * (rem / 6.0)
}

// ── Par score & target revision ───────────────────────────────────────────

/// DLS par score for Team 2 in a T20 chase.
///
/// Team 2 is "ahead on DLS" iff `actual_runs > par_score`, "level" iff equal,
/// behind otherwise. A T20 innings has 120 balls and 10 wickets of resource.
///
/// * `t1_total` — Team 1's completed first-innings total (runs)
/// * `balls_used_t2` — legal deliveries bowled in the chase so far
/// * `wickets_lost_t2` — wickets fallen in the chase so far
pub fn par_score_t20(t1_total: u32, balls_used_t2: u16, wickets_lost_t2: u8) -> f64 {
    let r1_full = resource_t20(T20_MAX_BALLS, 0);
    let balls_remaining = T20_MAX_BALLS.saturating_sub(balls_used_t2);
    let r2_remaining = resource_t20(balls_remaining, wickets_lost_t2);
    let r2_used = (r1_full - r2_remaining).max(0.0);
    (t1_total as f64) * r2_used / r1_full
}

/// Revised target (runs to win) when Team 2's innings is curtailed to
/// `new_max_balls` balls before any have been bowled.
///
/// Returns the integer score Team 2 needs to *exceed* par; i.e. the
/// standard "X to win" figure announced by the umpires.
pub fn revised_target_t20(t1_total: u32, new_max_balls: u16) -> u32 {
    let r1 = resource_t20(T20_MAX_BALLS, 0);
    let r2 = resource_t20(new_max_balls.min(T20_MAX_BALLS), 0);
    let par = (t1_total as f64) * r2 / r1;
    (par.floor() as u32) + 1
}

// ── Cumulative innings state ──────────────────────────────────────────────

/// Running total for one innings: runs, wickets, legal deliveries bowled.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct InningsState {
    pub runs: u32,
    pub wickets: u8,
    pub balls_used: u16,
}

impl InningsState {
    /// Apply a ball-by-ball [`CricketSignal`] to this innings. Returns true
    /// if the signal consumed a legal delivery (runs or wicket), false for
    /// wides / no-balls / non-ball events.
    pub fn apply(&mut self, signal: &CricketSignal) -> bool {
        match signal {
            CricketSignal::Runs(r) => {
                self.runs += *r as u32;
                self.balls_used = (self.balls_used + 1).min(T20_MAX_BALLS);
                true
            }
            CricketSignal::Wicket(extra) => {
                self.runs += *extra as u32;
                self.wickets = (self.wickets + 1).min(10);
                self.balls_used = (self.balls_used + 1).min(T20_MAX_BALLS);
                true
            }
            // Wide / no-ball: 1 penalty run + any extras, not a legal ball.
            CricketSignal::Wide(extra) | CricketSignal::NoBall(extra) => {
                self.runs += 1 + *extra as u32;
                false
            }
            CricketSignal::InningsOver | CricketSignal::MatchOver => false,
        }
    }
}

// ── Two-innings state machine ─────────────────────────────────────────────

/// Full T20 DLS state machine. Consumes [`CricketSignal`]s in order and
/// computes live par whenever the chasing side is at the crease.
#[derive(Debug, Clone)]
pub struct DlsEngine {
    pub current_innings: u8,
    pub innings_1: InningsState,
    pub innings_2: InningsState,
}

impl Default for DlsEngine {
    fn default() -> Self {
        Self::new_t20()
    }
}

impl DlsEngine {
    /// Fresh T20 engine: both innings empty, currently in innings 1.
    pub fn new_t20() -> Self {
        Self {
            current_innings: 1,
            innings_1: InningsState::default(),
            innings_2: InningsState::default(),
        }
    }

    /// Seed the engine mid-match. Useful when the taker boots after play has
    /// already started (e.g. wiring the DLS signal into a match already in
    /// progress).
    pub fn seed(
        innings_1: InningsState,
        innings_2: InningsState,
        current_innings: u8,
    ) -> Self {
        Self {
            current_innings: current_innings.clamp(1, 2),
            innings_1,
            innings_2,
        }
    }

    /// Apply a cricket signal to the engine. Routes ball events to the
    /// current innings and handles innings-boundary transitions.
    pub fn apply(&mut self, signal: &CricketSignal) {
        match signal {
            CricketSignal::InningsOver => {
                if self.current_innings == 1 {
                    self.current_innings = 2;
                }
            }
            CricketSignal::MatchOver => { /* terminal, no state change */ }
            _ => {
                let target = if self.current_innings == 1 {
                    &mut self.innings_1
                } else {
                    &mut self.innings_2
                };
                target.apply(signal);
            }
        }
    }

    /// Live par score for Team 2 at the current state. Returns `None` while
    /// still in the first innings or before Team 1 has faced a ball.
    pub fn par(&self) -> Option<f64> {
        if self.current_innings != 2 {
            return None;
        }
        if self.innings_1.balls_used == 0 && self.innings_1.runs == 0 {
            return None;
        }
        Some(par_score_t20(
            self.innings_1.runs,
            self.innings_2.balls_used,
            self.innings_2.wickets,
        ))
    }

    /// `actual_runs_t2 - par`. Positive → Team 2 ahead on DLS. `None`
    /// during the first innings.
    pub fn par_diff(&self) -> Option<f64> {
        self.par().map(|p| self.innings_2.runs as f64 - p)
    }

    /// Whether the match has crossed the T20 minimum-overs threshold for
    /// a DLS result to be declared (5 overs / 30 balls in the chase).
    pub fn is_result_valid(&self) -> bool {
        self.current_innings == 2 && self.innings_2.balls_used >= T20_MIN_CHASE_BALLS_FOR_RESULT
    }

    /// Human-readable snapshot of the current DLS state. Shape:
    /// `"T1=180 | T2=45/2 (32b) par=52.3 diff=-7.3 valid=yes"`.
    pub fn describe(&self) -> String {
        if self.current_innings == 1 {
            format!(
                "innings 1: {}/{} ({}b)",
                self.innings_1.runs, self.innings_1.wickets, self.innings_1.balls_used
            )
        } else {
            let par_s = self
                .par()
                .map(|p| format!("{p:.1}"))
                .unwrap_or_else(|| "-".to_string());
            let diff_s = self
                .par_diff()
                .map(|d| format!("{d:+.1}"))
                .unwrap_or_else(|| "-".to_string());
            format!(
                "T1={} | T2={}/{} ({}b) par={} diff={} valid={}",
                self.innings_1.runs,
                self.innings_2.runs,
                self.innings_2.wickets,
                self.innings_2.balls_used,
                par_s,
                diff_s,
                if self.is_result_valid() { "yes" } else { "no" },
            )
        }
    }
}

// ── Tests ─────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn approx(a: f64, b: f64, tol: f64) -> bool {
        (a - b).abs() <= tol
    }

    #[test]
    fn table_corners() {
        // Full innings, all wickets in hand = 100%
        assert!(approx(resource_t20(120, 0), 100.0, 1e-9));
        // Exhausted resources
        assert!(approx(resource_t20(0, 0), 0.0, 1e-9));
        assert!(approx(resource_t20(0, 9), 0.0, 1e-9));
        // 9 wickets down at start of innings = 8.3%
        assert!(approx(resource_t20(120, 9), 8.3, 1e-9));
    }

    #[test]
    fn table_mid_row_values_match_csv() {
        // Spot-check several published values from the ICC T20 Standard table
        assert!(approx(resource_t20(60, 0), 56.7, 1e-9));
        assert!(approx(resource_t20(60, 5), 46.1, 1e-9));
        assert!(approx(resource_t20(30, 3), 29.2, 1e-9));
        assert!(approx(resource_t20(90, 4), 66.4, 1e-9));
        assert!(approx(resource_t20(6, 0), 6.4, 1e-9));
    }

    #[test]
    fn resource_interpolates_within_over() {
        // Halfway between 60 balls (56.7) and 66 balls (61.7) at 0 wickets
        let mid = resource_t20(63, 0);
        assert!(approx(mid, (56.7 + 61.7) / 2.0, 1e-9));
    }

    #[test]
    fn par_score_at_chase_start_is_zero() {
        assert!(approx(par_score_t20(180, 0, 0), 0.0, 1e-9));
    }

    #[test]
    fn par_score_at_chase_end_is_t1_total() {
        // Chase finishes all 120 balls with no wickets: par = t1_total
        assert!(approx(par_score_t20(180, 120, 0), 180.0, 1e-9));
    }

    #[test]
    fn par_score_half_over_boundary() {
        // Standard sanity: 180 target, 10 overs used, 0 wickets
        // Resources used = 100 - 56.7 = 43.3
        // Par = 180 * 43.3 / 100 = 77.94
        let par = par_score_t20(180, 60, 0);
        assert!(approx(par, 77.94, 0.01), "par = {par}");
    }

    #[test]
    fn par_score_accounts_for_wickets() {
        // Losing wickets deplete resources faster → par is higher for the same balls
        let par_no_wickets = par_score_t20(180, 60, 0);
        let par_five_down = par_score_t20(180, 60, 5);
        assert!(
            par_five_down > par_no_wickets,
            "par should rise as wickets fall: {par_five_down} vs {par_no_wickets}"
        );
    }

    #[test]
    fn revised_target_full_innings_matches_t1_plus_one() {
        // Full 20 overs to chase 180 = 181 to win
        assert_eq!(revised_target_t20(180, 120), 181);
    }

    #[test]
    fn revised_target_reduced_overs() {
        // Chase 180 in 10 overs instead of 20
        // Resources = 56.7%, par = 180 * 56.7 / 100 = 102.06
        // Revised target = floor(102.06) + 1 = 103
        assert_eq!(revised_target_t20(180, 60), 103);
    }

    #[test]
    fn innings_state_tracks_runs_and_balls() {
        let mut s = InningsState::default();
        s.apply(&CricketSignal::Runs(4));
        s.apply(&CricketSignal::Runs(1));
        s.apply(&CricketSignal::Runs(0));
        assert_eq!(s.runs, 5);
        assert_eq!(s.wickets, 0);
        assert_eq!(s.balls_used, 3);
    }

    #[test]
    fn innings_state_tracks_wickets() {
        let mut s = InningsState::default();
        s.apply(&CricketSignal::Runs(6));
        s.apply(&CricketSignal::Wicket(0));
        s.apply(&CricketSignal::Wicket(2)); // run out for 2
        assert_eq!(s.runs, 8);
        assert_eq!(s.wickets, 2);
        assert_eq!(s.balls_used, 3);
    }

    #[test]
    fn innings_state_wide_no_ball_consume_no_legal_ball() {
        let mut s = InningsState::default();
        s.apply(&CricketSignal::Wide(0)); // +1
        s.apply(&CricketSignal::NoBall(4)); // +1 + 4 = 5
        s.apply(&CricketSignal::Runs(2));
        assert_eq!(s.runs, 8);
        assert_eq!(s.balls_used, 1);
        assert_eq!(s.wickets, 0);
    }

    #[test]
    fn innings_state_balls_capped_at_max() {
        let mut s = InningsState::default();
        for _ in 0..130 {
            s.apply(&CricketSignal::Runs(1));
        }
        assert_eq!(s.balls_used, T20_MAX_BALLS);
    }

    #[test]
    fn engine_routes_signals_to_current_innings() {
        let mut eng = DlsEngine::new_t20();
        eng.apply(&CricketSignal::Runs(4));
        eng.apply(&CricketSignal::Runs(6));
        eng.apply(&CricketSignal::InningsOver);
        eng.apply(&CricketSignal::Runs(1));

        assert_eq!(eng.innings_1.runs, 10);
        assert_eq!(eng.innings_1.balls_used, 2);
        assert_eq!(eng.innings_2.runs, 1);
        assert_eq!(eng.innings_2.balls_used, 1);
        assert_eq!(eng.current_innings, 2);
    }

    #[test]
    fn engine_par_none_during_innings_1() {
        let mut eng = DlsEngine::new_t20();
        eng.apply(&CricketSignal::Runs(4));
        assert!(eng.par().is_none());
        assert!(eng.par_diff().is_none());
    }

    #[test]
    fn engine_par_computed_during_chase() {
        let mut eng = DlsEngine::new_t20();
        // Innings 1: 10 runs off 2 balls (dummy)
        eng.apply(&CricketSignal::Runs(4));
        eng.apply(&CricketSignal::Runs(6));
        // Force innings boundary; seed innings 1 total via direct state mutation
        eng.innings_1.runs = 180;
        eng.innings_1.balls_used = 120;
        eng.apply(&CricketSignal::InningsOver);

        // After 10 overs, 0 wickets: expect par ≈ 77.94
        for _ in 0..60 {
            eng.apply(&CricketSignal::Runs(1));
        }
        let par = eng.par().expect("should have par in chase");
        assert!(approx(par, 77.94, 0.01), "par = {par}");
    }

    #[test]
    fn engine_result_valid_threshold() {
        let mut eng = DlsEngine::new_t20();
        eng.innings_1.runs = 180;
        eng.innings_1.balls_used = 120;
        eng.apply(&CricketSignal::InningsOver);

        // Under 5 overs: not valid
        for _ in 0..29 {
            eng.apply(&CricketSignal::Runs(1));
        }
        assert!(!eng.is_result_valid());

        // Cross the 5-over threshold
        eng.apply(&CricketSignal::Runs(1));
        assert!(eng.is_result_valid());
    }

    #[test]
    fn engine_seed_restores_state() {
        let i1 = InningsState { runs: 180, wickets: 10, balls_used: 120 };
        let i2 = InningsState { runs: 45, wickets: 2, balls_used: 48 };
        let eng = DlsEngine::seed(i1.clone(), i2.clone(), 2);

        assert_eq!(eng.current_innings, 2);
        assert_eq!(eng.innings_1, i1);
        assert_eq!(eng.innings_2, i2);
        let par = eng.par().expect("seeded chase should have par");
        // 48 balls used, 2 wickets: resources_remaining = resource_t20(72, 2) = 63.3
        // used = 100 - 63.3 = 36.7; par = 180 * 36.7 / 100 = 66.06
        assert!(approx(par, 66.06, 0.01), "par = {par}");
    }
}
