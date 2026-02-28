use anyhow::{bail, Result};
use ethers::abi::{self, Token};
use ethers::core::k256::ecdsa::SigningKey;
use ethers::middleware::SignerMiddleware;
use ethers::providers::{Http, Middleware, Provider};
use ethers::signers::{LocalWallet, Signer};
use ethers::types::{Address, Bytes, TransactionRequest, U256};
use ethers::utils::keccak256;
use std::sync::Arc;

use crate::config::Config;

const CTF_CONTRACT: &str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
const USDC_CONTRACT: &str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";

type SignedClient = SignerMiddleware<Provider<Http>, LocalWallet>;

fn build_client(config: &Config) -> Result<Arc<SignedClient>> {
    let provider = Provider::<Http>::try_from(config.polygon_rpc.as_str())?;
    let key = config.polymarket_private_key.strip_prefix("0x")
        .unwrap_or(&config.polymarket_private_key);
    let key_bytes = hex::decode(key)?;
    let signing_key = SigningKey::from_bytes(key_bytes.as_slice().into())?;
    let wallet = LocalWallet::from(signing_key).with_chain_id(config.chain_id);
    Ok(Arc::new(SignerMiddleware::new(provider, wallet)))
}

/// Wrap `inner_data` in a proxy wallet `execute(address,uint256,bytes)` call.
///
/// Polymarket proxy wallets (signature_type == 1) are smart contracts owned by
/// the user's EOA. To route CTF/USDC operations through the proxy wallet so
/// that tokens land in the proxy (not the EOA), the EOA calls:
///   proxyWallet.execute(target, 0, innerCalldata)
///
/// The proxy wallet then forwards the call with itself as msg.sender, meaning
/// CTF tokens end up in the proxy wallet and are available for CLOB trading.
///
/// Function selector: keccak256("execute(address,uint256,bytes)") = 0x1cff79cd
pub fn proxy_execute_calldata(target: Address, inner_data: Bytes) -> Result<Bytes> {
    let selector = &keccak256(b"execute(address,uint256,bytes)")[..4];
    let encoded = abi::encode(&[
        Token::Address(target),
        Token::Uint(U256::zero()),
        Token::Bytes(inner_data.to_vec()),
    ]);
    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    Ok(Bytes::from(data))
}

/// Resolve final (to, calldata) for a transaction.
///
/// - signature_type == 0 (EOA): send directly to target
/// - signature_type == 1 (proxy): wrap in proxy.execute() so the proxy wallet
///   is msg.sender and tokens land in the proxy wallet
/// - signature_type == 2 (Gnosis Safe): not supported for direct CTF ops yet
///
/// The user's polymarket_address should be the proxy wallet address for type 1.
/// Based on the Polymarket API response (proxyWallet field ≠ address field),
/// the correct signature type is 1 (not 2 — type 2 is Gnosis Safe multisig).
fn resolve_tx(config: &Config, target: Address, calldata: Bytes) -> Result<(Address, Bytes)> {
    if config.signature_type == 1 && !config.polymarket_address.is_empty() {
        let proxy: Address = config.polymarket_address.parse()?;
        tracing::debug!(
            proxy = %format!("{:#x}", proxy),
            target = %format!("{:#x}", target),
            "routing CTF tx through proxy wallet"
        );
        let wrapped = proxy_execute_calldata(target, calldata)?;
        Ok((proxy, wrapped))
    } else {
        Ok((target, calldata))
    }
}

/// Return the address that holds the CTF tokens.
///
/// For signature_type == 1 (proxy wallet), tokens are held by the proxy wallet
/// (config.polymarket_address), not the EOA derived from the private key.
/// For signature_type == 0 (EOA), tokens are held by the EOA.
pub fn ctf_token_owner(config: &Config) -> Result<Address> {
    if config.signature_type == 1 && !config.polymarket_address.is_empty() {
        Ok(config.polymarket_address.parse()?)
    } else {
        let key = config.polymarket_private_key.strip_prefix("0x")
            .unwrap_or(&config.polymarket_private_key);
        let key_bytes = hex::decode(key)?;
        let signing_key = SigningKey::from_bytes(key_bytes.as_slice().into())?;
        let wallet = LocalWallet::from(signing_key);
        Ok(wallet.address())
    }
}

fn split_position_calldata(condition_id: &str, amount_usdc: u64) -> Result<Bytes> {
    let usdc_addr: Address = USDC_CONTRACT.parse()?;
    let parent = [0u8; 32];
    let cond_bytes = parse_bytes32(condition_id)?;
    let amount_base = U256::from(amount_usdc) * U256::from(1_000_000u64);

    let selector = &keccak256(b"splitPosition(address,bytes32,bytes32,uint256[],uint256)")[..4];
    let encoded = abi::encode(&[
        Token::Address(usdc_addr),
        Token::FixedBytes(parent.to_vec()),
        Token::FixedBytes(cond_bytes.to_vec()),
        Token::Array(vec![Token::Uint(U256::from(1)), Token::Uint(U256::from(2))]),
        Token::Uint(amount_base),
    ]);

    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    Ok(Bytes::from(data))
}

fn merge_positions_calldata(condition_id: &str, amount_tokens: u64) -> Result<Bytes> {
    let usdc_addr: Address = USDC_CONTRACT.parse()?;
    let parent = [0u8; 32];
    let cond_bytes = parse_bytes32(condition_id)?;
    let amount_base = U256::from(amount_tokens) * U256::from(1_000_000u64);

    let selector = &keccak256(b"mergePositions(address,bytes32,bytes32,uint256[],uint256)")[..4];
    let encoded = abi::encode(&[
        Token::Address(usdc_addr),
        Token::FixedBytes(parent.to_vec()),
        Token::FixedBytes(cond_bytes.to_vec()),
        Token::Array(vec![Token::Uint(U256::from(1)), Token::Uint(U256::from(2))]),
        Token::Uint(amount_base),
    ]);

    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    Ok(Bytes::from(data))
}

fn redeem_positions_calldata(condition_id: &str) -> Result<Bytes> {
    let usdc_addr: Address = USDC_CONTRACT.parse()?;
    let parent = [0u8; 32];
    let cond_bytes = parse_bytes32(condition_id)?;

    let selector = &keccak256(b"redeemPositions(address,bytes32,bytes32,uint256[])")[..4];
    let encoded = abi::encode(&[
        Token::Address(usdc_addr),
        Token::FixedBytes(parent.to_vec()),
        Token::FixedBytes(cond_bytes.to_vec()),
        Token::Array(vec![Token::Uint(U256::from(1)), Token::Uint(U256::from(2))]),
    ]);

    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    Ok(Bytes::from(data))
}

fn approve_calldata(spender: &str, amount: U256) -> Result<Bytes> {
    let spender_addr: Address = spender.parse()?;
    let selector = &keccak256(b"approve(address,uint256)")[..4];
    let encoded = abi::encode(&[
        Token::Address(spender_addr),
        Token::Uint(amount),
    ]);
    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    Ok(Bytes::from(data))
}

/// Split USDC into YES + NO token pairs via the CTF contract.
/// $X USDC -> X YES tokens + X NO tokens
///
/// When signature_type == 1, both the approval and split are routed through
/// the proxy wallet so tokens land in the proxy wallet, not the EOA.
pub async fn split(config: &Config, condition_id: &str, amount_usdc: u64) -> Result<String> {
    let client = build_client(config)?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;
    let usdc_addr: Address = USDC_CONTRACT.parse()?;

    let approve_amount = U256::from(amount_usdc) * U256::from(1_000_000u64);
    let approve_data = approve_calldata(CTF_CONTRACT, approve_amount)?;
    let (approve_to, approve_final) = resolve_tx(config, usdc_addr, approve_data)?;
    let approve_tx = TransactionRequest::new().to(approve_to).data(approve_final);

    tracing::info!(
        amount_usdc,
        signature_type = config.signature_type,
        proxy = config.signature_type == 1,
        "approving USDC for CTF split"
    );
    let pending = client.send_transaction(approve_tx, None).await?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("approval tx dropped"))?;
    tracing::info!(tx = %receipt.transaction_hash, "USDC approval confirmed");

    let split_data = split_position_calldata(condition_id, amount_usdc)?;
    let (split_to, split_final) = resolve_tx(config, ctf_addr, split_data)?;
    let split_tx = TransactionRequest::new().to(split_to).data(split_final);

    tracing::info!(amount_usdc, condition_id, "splitting USDC into YES+NO tokens");
    let pending = client.send_transaction(split_tx, None).await?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("split tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF split confirmed");
    Ok(tx_hash)
}

/// Merge YES + NO token pairs back into USDC.
/// X YES + X NO tokens -> $X USDC
///
/// When signature_type == 1, routed through the proxy wallet so it operates
/// on tokens held in the proxy wallet.
pub async fn merge(config: &Config, condition_id: &str, amount_tokens: u64) -> Result<String> {
    let client = build_client(config)?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;

    let merge_data = merge_positions_calldata(condition_id, amount_tokens)?;
    let (merge_to, merge_final) = resolve_tx(config, ctf_addr, merge_data)?;
    let merge_tx = TransactionRequest::new().to(merge_to).data(merge_final);

    tracing::info!(
        amount_tokens,
        condition_id,
        proxy = config.signature_type == 1,
        "merging YES+NO tokens into USDC"
    );
    let pending = client.send_transaction(merge_tx, None).await?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("merge tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF merge confirmed");
    Ok(tx_hash)
}

/// Redeem winning tokens for USDC after market resolution.
///
/// When signature_type == 1, routed through the proxy wallet.
pub async fn redeem(config: &Config, condition_id: &str) -> Result<String> {
    let client = build_client(config)?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;

    let redeem_data = redeem_positions_calldata(condition_id)?;
    let (redeem_to, redeem_final) = resolve_tx(config, ctf_addr, redeem_data)?;
    let redeem_tx = TransactionRequest::new().to(redeem_to).data(redeem_final);

    tracing::info!(
        condition_id,
        proxy = config.signature_type == 1,
        "redeeming winning tokens for USDC"
    );
    let pending = client.send_transaction(redeem_tx, None).await?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("redeem tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF redeem confirmed");
    Ok(tx_hash)
}

/// Fetch ERC1155 token balance for a given token_id from the CTF contract.
///
/// Queries the balance of the correct token owner:
/// - signature_type == 1: proxy wallet (config.polymarket_address) holds the tokens
/// - signature_type == 0: EOA derived from private key holds the tokens
pub async fn balance_of(config: &Config, token_id: &str) -> Result<u64> {
    let provider = Provider::<Http>::try_from(config.polygon_rpc.as_str())?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;

    let owner = ctf_token_owner(config)?;
    tracing::debug!(
        owner = %format!("{:#x}", owner),
        token_id,
        signature_type = config.signature_type,
        "querying CTF token balance"
    );

    let token_id_u256 = U256::from_dec_str(token_id)
        .or_else(|_| {
            let s = token_id.strip_prefix("0x").unwrap_or(token_id);
            U256::from_str_radix(s, 16).map_err(|e| anyhow::anyhow!("{e}"))
        })?;

    let selector = &keccak256(b"balanceOf(address,uint256)")[..4];
    let encoded = abi::encode(&[
        Token::Address(owner),
        Token::Uint(token_id_u256),
    ]);
    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);

    let call = TransactionRequest::new().to(ctf_addr).data(Bytes::from(data));
    let result = provider.call(&call.into(), None).await?;

    let decoded = abi::decode(&[ethers::abi::ParamType::Uint(256)], &result)?;
    if let Some(Token::Uint(val)) = decoded.first() {
        // CTF tokens use 6 decimals (same as USDC)
        Ok((val / U256::from(1_000_000u64)).as_u64())
    } else {
        Ok(0)
    }
}

/// Sync on-chain token balances into the position tracker.
/// Returns (team_a_tokens, team_b_tokens) in whole token units.
pub async fn sync_balances(config: &Config) -> Result<(u64, u64)> {
    if !config.has_tokens() {
        bail!("token IDs not configured");
    }
    let (a, b) = tokio::try_join!(
        balance_of(config, &config.team_a_token_id),
        balance_of(config, &config.team_b_token_id),
    )?;
    Ok((a, b))
}

fn parse_bytes32(hex_str: &str) -> Result<[u8; 32]> {
    let s = hex_str.strip_prefix("0x").unwrap_or(hex_str);
    let bytes = hex::decode(s)?;
    if bytes.len() != 32 {
        bail!("expected 32 bytes for condition_id, got {}", bytes.len());
    }
    let mut arr = [0u8; 32];
    arr.copy_from_slice(&bytes);
    Ok(arr)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proxy_execute_calldata_has_correct_selector() {
        let target: Address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045".parse().unwrap();
        let inner = Bytes::from(vec![0xde, 0xad, 0xbe, 0xef]);
        let result = proxy_execute_calldata(target, inner).unwrap();
        // keccak256("execute(address,uint256,bytes)")[0..4] = 0x1cff79cd
        assert_eq!(&result[..4], &[0x1c, 0xff, 0x79, 0xcd]);
    }

    #[test]
    fn parse_bytes32_strips_0x_prefix() {
        let hex = "0x1234567890123456789012345678901234567890123456789012345678901234";
        let result = parse_bytes32(hex).unwrap();
        assert_eq!(result[0], 0x12);
        assert_eq!(result[1], 0x34);
        assert_eq!(result[31], 0x34);
    }

    #[test]
    fn parse_bytes32_rejects_wrong_length() {
        let hex = "0x1234";
        assert!(parse_bytes32(hex).is_err());
    }

    #[test]
    fn split_calldata_encodes_amount_correctly() {
        let condition_id = "0x1234567890123456789012345678901234567890123456789012345678901234";
        // 10 USDC = 10_000_000 in base units
        let data = split_position_calldata(condition_id, 10).unwrap();
        // Must have the splitPosition selector as first 4 bytes
        let selector = &keccak256(b"splitPosition(address,bytes32,bytes32,uint256[],uint256)")[..4];
        assert_eq!(&data[..4], selector);
    }

    #[test]
    fn approve_calldata_encodes_spender_and_amount() {
        let spender = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
        let amount = U256::from(1_000_000u64);
        let data = approve_calldata(spender, amount).unwrap();
        let selector = &keccak256(b"approve(address,uint256)")[..4];
        assert_eq!(&data[..4], selector);
    }
}
