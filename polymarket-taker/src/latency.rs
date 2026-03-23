use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LatencyMetric {
    SignalToDecision,
    SignToPost,
    PostToResponse,
    FillDetectWs,
    FillDetectPoll,
    E2eSignalToFill,
}

impl LatencyMetric {
    pub fn name(&self) -> &'static str {
        match self {
            Self::SignalToDecision => "signal_to_decision",
            Self::SignToPost => "sign_to_post",
            Self::PostToResponse => "post_to_response",
            Self::FillDetectWs => "fill_detect_ws",
            Self::FillDetectPoll => "fill_detect_poll",
            Self::E2eSignalToFill => "e2e_signal_to_fill",
        }
    }

    fn all() -> &'static [Self] {
        &[
            Self::SignalToDecision,
            Self::SignToPost,
            Self::PostToResponse,
            Self::FillDetectWs,
            Self::FillDetectPoll,
            Self::E2eSignalToFill,
        ]
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Percentiles {
    pub count: usize,
    pub p50_us: u64,
    pub p95_us: u64,
    pub p99_us: u64,
    pub min_us: u64,
    pub max_us: u64,
}

impl Default for Percentiles {
    fn default() -> Self {
        Self { count: 0, p50_us: 0, p95_us: 0, p99_us: 0, min_us: 0, max_us: 0 }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct LatencySnapshot {
    pub signal_to_decision: Percentiles,
    pub sign_to_post: Percentiles,
    pub post_to_response: Percentiles,
    pub fill_detect_ws: Percentiles,
    pub fill_detect_poll: Percentiles,
    pub e2e_signal_to_fill: Percentiles,
}

pub struct LatencyTracker {
    buckets: [Mutex<Vec<u64>>; 6],
}

impl LatencyTracker {
    pub fn new() -> Self {
        Self {
            buckets: [
                Mutex::new(Vec::new()),
                Mutex::new(Vec::new()),
                Mutex::new(Vec::new()),
                Mutex::new(Vec::new()),
                Mutex::new(Vec::new()),
                Mutex::new(Vec::new()),
            ],
        }
    }

    fn index(metric: LatencyMetric) -> usize {
        match metric {
            LatencyMetric::SignalToDecision => 0,
            LatencyMetric::SignToPost => 1,
            LatencyMetric::PostToResponse => 2,
            LatencyMetric::FillDetectWs => 3,
            LatencyMetric::FillDetectPoll => 4,
            LatencyMetric::E2eSignalToFill => 5,
        }
    }

    pub fn record(&self, metric: LatencyMetric, duration: Duration) {
        let us = duration.as_micros() as u64;
        self.buckets[Self::index(metric)].lock().unwrap().push(us);
    }

    pub fn percentiles(&self, metric: LatencyMetric) -> Percentiles {
        let bucket = self.buckets[Self::index(metric)].lock().unwrap();
        compute_percentiles(&bucket)
    }

    pub fn snapshot(&self) -> LatencySnapshot {
        let metrics = LatencyMetric::all();
        let pcts: Vec<Percentiles> = metrics.iter()
            .map(|m| self.percentiles(*m))
            .collect();
        LatencySnapshot {
            signal_to_decision: pcts[0].clone(),
            sign_to_post: pcts[1].clone(),
            post_to_response: pcts[2].clone(),
            fill_detect_ws: pcts[3].clone(),
            fill_detect_poll: pcts[4].clone(),
            e2e_signal_to_fill: pcts[5].clone(),
        }
    }
}

fn compute_percentiles(values: &[u64]) -> Percentiles {
    if values.is_empty() {
        return Percentiles::default();
    }
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    let n = sorted.len();
    Percentiles {
        count: n,
        p50_us: sorted[n * 50 / 100],
        p95_us: sorted[(n * 95 / 100).min(n - 1)],
        p99_us: sorted[(n * 99 / 100).min(n - 1)],
        min_us: sorted[0],
        max_us: sorted[n - 1],
    }
}
