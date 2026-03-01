/// Tests for CTF calldata generation and proxy wallet routing helpers.
/// All tests are pure (no network calls).
use crate::ctf::proxy_execute_calldata;
use ethers::types::{Address, Bytes};

// ── proxy_execute_calldata ────────────────────────────────────────────────────

#[test]
fn proxy_execute_has_correct_function_selector() {
    // keccak256("execute(address,uint256,bytes)")[0..4] = 0xb61d27f6
    let target: Address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045".parse().unwrap();
    let inner = Bytes::from(vec![0xde, 0xad, 0xbe, 0xef]);
    let result = proxy_execute_calldata(target, inner).unwrap();
    assert_eq!(
        &result[..4],
        &[0xb6, 0x1d, 0x27, 0xf6],
        "expected execute(address,uint256,bytes) selector 0xb61d27f6"
    );
}

#[test]
fn proxy_execute_calldata_is_longer_than_inner() {
    let target: Address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045".parse().unwrap();
    let inner = Bytes::from(vec![0x01, 0x02]);
    let result = proxy_execute_calldata(target, inner.clone()).unwrap();
    // 4 (selector) + 32 (address) + 32 (uint256) + 32 (offset) + 32 (len) + padded data
    assert!(result.len() > inner.len() + 4);
}

#[test]
fn proxy_execute_different_targets_produce_different_calldata() {
    let target_a: Address = "0x1111111111111111111111111111111111111111".parse().unwrap();
    let target_b: Address = "0x2222222222222222222222222222222222222222".parse().unwrap();
    let inner = Bytes::from(vec![0xaa]);
    let a = proxy_execute_calldata(target_a, inner.clone()).unwrap();
    let b = proxy_execute_calldata(target_b, inner).unwrap();
    assert_ne!(a, b);
}

#[test]
fn proxy_execute_different_inner_data_produce_different_calldata() {
    let target: Address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045".parse().unwrap();
    let inner_a = Bytes::from(vec![0x11, 0x22]);
    let inner_b = Bytes::from(vec![0x33, 0x44]);
    let a = proxy_execute_calldata(target, inner_a).unwrap();
    let b = proxy_execute_calldata(target, inner_b).unwrap();
    assert_ne!(a, b);
}

#[test]
fn proxy_execute_empty_inner_data_is_valid() {
    let target: Address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045".parse().unwrap();
    let result = proxy_execute_calldata(target, Bytes::default());
    assert!(result.is_ok());
    assert_eq!(&result.unwrap()[..4], &[0xb6, 0x1d, 0x27, 0xf6]);
}
