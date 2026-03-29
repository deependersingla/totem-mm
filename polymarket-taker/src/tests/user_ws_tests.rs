use rust_decimal_macros::dec;
use crate::types::Side;
use crate::user_ws::parse_user_ws_message;

#[test]
fn test_parse_trade_matched() {
    let json = r#"[{
        "event_type": "trade",
        "type": "trade",
        "taker_order_id": "0xabc123",
        "status": "MATCHED",
        "asset_id": "token_yes",
        "price": "0.65",
        "size": "50",
        "side": "BUY"
    }]"#;
    let events = parse_user_ws_message(json);
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].order_id, "0xabc123");
    assert_eq!(events[0].filled_size, dec!(50));
    assert_eq!(events[0].avg_price, dec!(0.65));
    assert_eq!(events[0].status, "MATCHED");
    assert_eq!(events[0].side, Side::Buy);
}

#[test]
fn test_parse_trade_confirmed() {
    let json = r#"{"event_type": "trade", "taker_order_id": "0xdef456", "status": "CONFIRMED", "asset_id": "t1", "price": "0.30", "size": "100", "side": "SELL"}"#;
    let events = parse_user_ws_message(json);
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].order_id, "0xdef456");
    assert_eq!(events[0].side, Side::Sell);
}

#[test]
fn test_parse_trade_retrying_ignored() {
    let json = r#"[{"event_type": "trade", "taker_order_id": "0x111", "status": "RETRYING", "asset_id": "t1", "price": "0.50", "size": "10", "side": "BUY"}]"#;
    let events = parse_user_ws_message(json);
    assert_eq!(events.len(), 0, "RETRYING status should not emit fill events");
}

#[test]
fn test_parse_trade_failed_ignored() {
    let json = r#"[{"event_type": "trade", "taker_order_id": "0x222", "status": "FAILED", "asset_id": "t1", "price": "0.50", "size": "10", "side": "BUY"}]"#;
    let events = parse_user_ws_message(json);
    assert_eq!(events.len(), 0, "FAILED status should not emit fill events");
}

#[test]
fn test_parse_order_with_fill() {
    let json = r#"[{"event_type": "order", "type": "order", "id": "0xorder1", "size_matched": "25", "original_size": "50", "price": "0.40", "side": "BUY", "asset_id": "token_a", "status": "open"}]"#;
    let events = parse_user_ws_message(json);
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].order_id, "0xorder1");
    assert_eq!(events[0].filled_size, dec!(25));
}

#[test]
fn test_parse_malformed_json() {
    let events = parse_user_ws_message("not json at all");
    assert!(events.is_empty());
}

#[test]
fn test_parse_empty_array() {
    let events = parse_user_ws_message("[]");
    assert!(events.is_empty());
}
