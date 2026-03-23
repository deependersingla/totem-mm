#[test]
fn test_heartbeat_endpoint_path() {
    // Verify the heartbeat path matches Polymarket docs
    assert_eq!(crate::heartbeat::HEARTBEAT_PATH, "/heartbeats");
}
