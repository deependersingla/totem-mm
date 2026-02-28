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

/// Wrap `inner_data` in a proxy wallet `execute(address,uint256,bytes)` call.
/// Selector: keccak256("execute(address,uint256,bytes)")[0..4] = 0xb61d27f6
pub(crate) fn proxy_execute_calldata(target: Address, inner_data: Bytes) -> Result<Bytes> {
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

fn build_client(config: &Config) -> Result<Arc<SignedClient>> {
    let provider = Provider::<Http>::try_from(config.polygon_rpc.as_str())?;
    let key = config.polymarket_private_key.strip_prefix("0x")
        .unwrap_or(&config.polymarket_private_key);
    let key_bytes = hex::decode(key)?;
    let signing_key = SigningKey::from_bytes(key_bytes.as_slice().into())?;
    let wallet = LocalWallet::from(signing_key).with_chain_id(config.chain_id);
    Ok(Arc::new(SignerMiddleware::new(provider, wallet)))
}

fn split_position_calldata(condition_id: &str, amount_usdc: u64) -> Result<Bytes> {
    let usdc_addr: Address = USDC_CONTRACT.parse()?;
    let parent = [0u8; 32];
    let cond_bytes = parse_bytes32(condition_id)?;
    let amount_base = U256::from(amount_usdc) * U256::from(1_000_000u64); // USDC 6 decimals

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
pub async fn split(config: &Config, condition_id: &str, amount_usdc: u64) -> Result<String> {
    let client = build_client(config)?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;
    let usdc_addr: Address = USDC_CONTRACT.parse()?;

    let approve_amount = U256::from(amount_usdc) * U256::from(1_000_000u64);
    let approve_data = approve_calldata(CTF_CONTRACT, approve_amount)?;
    let approve_tx = TransactionRequest::new().to(usdc_addr).data(approve_data);

    tracing::info!(amount_usdc, "approving USDC for CTF split");
    let pending = client.send_transaction(approve_tx, None).await?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("approval tx dropped"))?;
    tracing::info!(tx = %receipt.transaction_hash, "USDC approval confirmed");

    let split_data = split_position_calldata(condition_id, amount_usdc)?;
    let split_tx = TransactionRequest::new().to(ctf_addr).data(split_data);

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
pub async fn merge(config: &Config, condition_id: &str, amount_tokens: u64) -> Result<String> {
    let client = build_client(config)?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;

    let merge_data = merge_positions_calldata(condition_id, amount_tokens)?;
    let merge_tx = TransactionRequest::new().to(ctf_addr).data(merge_data);

    tracing::info!(amount_tokens, condition_id, "merging YES+NO tokens into USDC");
    let pending = client.send_transaction(merge_tx, None).await?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("merge tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF merge confirmed");
    Ok(tx_hash)
}

/// Redeem winning tokens for USDC after market resolution.
pub async fn redeem(config: &Config, condition_id: &str) -> Result<String> {
    let client = build_client(config)?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;

    let redeem_data = redeem_positions_calldata(condition_id)?;
    let redeem_tx = TransactionRequest::new().to(ctf_addr).data(redeem_data);

    tracing::info!(condition_id, "redeeming winning tokens for USDC");
    let pending = client.send_transaction(redeem_tx, None).await?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("redeem tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF redeem confirmed");
    Ok(tx_hash)
}

/// Fetch ERC1155 token balance for a given token_id from the CTF contract.
pub async fn balance_of(config: &Config, token_id: &str) -> Result<u64> {
    let provider = Provider::<Http>::try_from(config.polygon_rpc.as_str())?;
    let ctf_addr: Address = CTF_CONTRACT.parse()?;

    let key = config.polymarket_private_key.strip_prefix("0x")
        .unwrap_or(&config.polymarket_private_key);
    let key_bytes = hex::decode(key)?;
    let signing_key = SigningKey::from_bytes(key_bytes.as_slice().into())?;
    let wallet = LocalWallet::from(signing_key);
    let owner = wallet.address();

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
        // CTF tokens have same decimals as USDC.e (6)
        Ok((val / U256::from(1_000_000u64)).as_u64())
    } else {
        Ok(0)
    }
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
