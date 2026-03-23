use criterion::{black_box, criterion_group, criterion_main, Criterion};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::str::FromStr;

fn bench_to_base_units(c: &mut Criterion) {
    c.bench_function("to_base_units", |b| {
        b.iter(|| {
            // Simulates orders::to_base_units logic
            let price = black_box(dec!(0.65));
            let size = black_box(dec!(50));
            let maker = (size * Decimal::new(1_000_000, 0)).floor();
            let taker = (size * price * Decimal::new(1_000_000, 0)).floor();
            black_box((maker, taker));
        })
    });
}

fn bench_compute_amounts(c: &mut Criterion) {
    c.bench_function("compute_amounts", |b| {
        b.iter(|| {
            let price = black_box(dec!(0.65));
            let size = black_box(dec!(50));
            let scale = Decimal::new(1_000_000, 0);
            // BUY: maker_amount = size * price * scale, taker_amount = size * scale
            let maker = (size * price * scale).floor();
            let taker = (size * scale).floor();
            black_box((maker, taker));
        })
    });
}

fn bench_compute_size(c: &mut Criterion) {
    c.bench_function("compute_size", |b| {
        b.iter(|| {
            let available = black_box(dec!(100));
            let price = black_box(dec!(0.65));
            let max_usdc = black_box(dec!(10));
            let max_tokens = (max_usdc / price).floor();
            let size = available.min(max_tokens);
            black_box(size);
        })
    });
}

fn bench_orderbook_json_parse(c: &mut Criterion) {
    let sample = r#"[{"event_type":"book","asset_id":"12345","market":"0xabc","timestamp":"1708900000","bids":[{"price":"0.65","size":"100"},{"price":"0.64","size":"200"}],"asks":[{"price":"0.66","size":"150"},{"price":"0.67","size":"300"}],"hash":"abc123"}]"#;

    c.bench_function("orderbook_json_parse", |b| {
        b.iter(|| {
            let v: serde_json::Value = serde_json::from_str(black_box(sample)).unwrap();
            black_box(v);
        })
    });
}

criterion_group!(
    benches,
    bench_to_base_units,
    bench_compute_amounts,
    bench_compute_size,
    bench_orderbook_json_parse,
);
criterion_main!(benches);
