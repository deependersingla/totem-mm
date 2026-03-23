use std::time::Duration;
use crate::latency::{LatencyMetric, LatencyTracker};

#[test]
fn test_empty_tracker_returns_zeros() {
    let tracker = LatencyTracker::new();
    let p = tracker.percentiles(LatencyMetric::SignalToDecision);
    assert_eq!(p.count, 0);
    assert_eq!(p.p50_us, 0);
    assert_eq!(p.p95_us, 0);
    assert_eq!(p.p99_us, 0);
}

#[test]
fn test_record_and_percentiles() {
    let tracker = LatencyTracker::new();
    // Record 100 values: 1ms, 2ms, ..., 100ms
    for i in 1..=100 {
        tracker.record(LatencyMetric::SignalToDecision, Duration::from_millis(i));
    }
    let p = tracker.percentiles(LatencyMetric::SignalToDecision);
    assert_eq!(p.count, 100);
    // p50 = sorted[100*50/100] = sorted[50] = 51ms (0-indexed)
    assert_eq!(p.p50_us, 51_000);
    // p95 = sorted[min(95, 99)] = sorted[95] = 96ms
    assert_eq!(p.p95_us, 96_000);
    // p99 = sorted[min(99, 99)] = sorted[99] = 100ms
    assert_eq!(p.p99_us, 100_000);
    assert_eq!(p.min_us, 1_000);  // 1ms
    assert_eq!(p.max_us, 100_000); // 100ms
}

#[test]
fn test_snapshot_serializes() {
    let tracker = LatencyTracker::new();
    tracker.record(LatencyMetric::PostToResponse, Duration::from_millis(5));
    let snapshot = tracker.snapshot();
    let json = serde_json::to_string(&snapshot).unwrap();
    assert!(json.contains("post_to_response"));
    assert!(json.contains("signal_to_decision"));
}

#[test]
fn test_single_value() {
    let tracker = LatencyTracker::new();
    tracker.record(LatencyMetric::FillDetectWs, Duration::from_micros(500));
    let p = tracker.percentiles(LatencyMetric::FillDetectWs);
    assert_eq!(p.count, 1);
    assert_eq!(p.p50_us, 500);
    assert_eq!(p.min_us, 500);
    assert_eq!(p.max_us, 500);
}
