use anyhow::{bail, Result};
use ethers::abi::{self, Token};
use ethers::core::k256::ecdsa::SigningKey;
use ethers::middleware::SignerMiddleware;
use ethers::providers::{Http, Middleware, Provider};
use ethers::signers::{LocalWallet, Signer};
use ethers::types::{Address, Bytes, TransactionRequest, U256};
use ethers::utils::keccak256;
use rust_decimal::Decimal;
use std::sync::Arc;

use crate::config::Config;

/// Standard ConditionalTokens contract (Polygon mainnet).
const CTF_CONTRACT: &str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
/// Neg-risk NegRiskAdapter contract (Polygon mainnet).
/// Neg-risk markets route split/merge/redeem through this adapter, NOT the
/// standard CTF contract. Using the wrong contract will revert the transaction.
const NEG_RISK_CTF_CONTRACT: &str = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296";
const USDC_CONTRACT: &str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";

fn ctf_contract(config: &Config) -> &'static str {
    if config.neg_risk { NEG_RISK_CTF_CONTRACT } else { CTF_CONTRACT }
}

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

/// Pre-flight check: ensure EOA has enough MATIC/POL for gas.
async fn check_gas_balance(client: &SignedClient) -> Result<()> {
    let eoa = client.address();
    let balance = client.provider().get_balance(eoa, None).await
        .map_err(|e| anyhow::anyhow!("failed to check MATIC balance: {e}"))?;
    let min_gas = U256::from(1_000_000_000_000_000u64); // 0.001 MATIC
    if balance < min_gas {
        let bal_str = ethers::utils::format_ether(balance);
        bail!("EOA {:#x} has only {} MATIC — need gas for on-chain tx. Send some POL/MATIC to this address.", eoa, bal_str);
    }
    Ok(())
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
/// Function selector: keccak256("execute(address,uint256,bytes)") = 0xb61d27f6
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
/// - signature_type == 1 (POLY_PROXY) or 2 (GNOSIS_SAFE): wrap in
///   proxy.execute() so the proxy wallet is msg.sender and tokens land in
///   the proxy wallet, not the EOA.
fn resolve_tx(config: &Config, target: Address, calldata: Bytes) -> Result<(Address, Bytes)> {
    if config.signature_type > 0 && !config.polymarket_address.is_empty() {
        let proxy: Address = config.polymarket_address.parse()?;
        tracing::debug!(
            proxy = %format!("{:#x}", proxy),
            target = %format!("{:#x}", target),
            signature_type = config.signature_type,
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
/// For signature_type 1 (POLY_PROXY) or 2 (GNOSIS_SAFE), tokens are held by
/// the proxy wallet (config.polymarket_address), not the EOA.
/// For signature_type 0 (EOA), tokens are held by the EOA.
pub fn ctf_token_owner(config: &Config) -> Result<Address> {
    if config.signature_type > 0 && !config.polymarket_address.is_empty() {
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
/// Split always runs directly from the EOA (no proxy.execute() wrapping),
/// because the proxy wallet cannot hold MATIC for gas. After splitting,
/// use "Move Tokens → Proxy" to transfer tokens to the proxy for CLOB trading.
pub async fn split(config: &Config, condition_id: &str, amount_usdc: u64) -> Result<String> {
    let client = build_client(config)?;
    check_gas_balance(&client).await?;
    let ctf = ctf_contract(config);
    let ctf_addr: Address = ctf.parse()?;
    let usdc_addr: Address = USDC_CONTRACT.parse()?;

    // Approve the correct CTF contract (standard or neg-risk) as USDC spender
    // Sent directly from EOA — no proxy wrapping
    let approve_amount = U256::from(amount_usdc) * U256::from(1_000_000u64);
    let approve_data = approve_calldata(ctf, approve_amount)?;
    let approve_tx = TransactionRequest::new().to(usdc_addr).data(approve_data);

    tracing::info!(
        amount_usdc,
        ctf_contract = ctf,
        eoa = %format!("{:#x}", client.address()),
        "approving USDC for CTF split (EOA direct)"
    );
    let pending = client.send_transaction(approve_tx, None).await
        .map_err(|e| anyhow::anyhow!("approve tx send failed (check MATIC and USDC balance in EOA): {e}"))?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("approval tx dropped"))?;
    tracing::info!(tx = %receipt.transaction_hash, "USDC approval confirmed");

    // Split directly from EOA — tokens land in EOA
    let split_data = split_position_calldata(condition_id, amount_usdc)?;
    let split_tx = TransactionRequest::new().to(ctf_addr).data(split_data);

    tracing::info!(amount_usdc, condition_id, "splitting USDC into YES+NO tokens (EOA direct)");
    let pending = client.send_transaction(split_tx, None).await
        .map_err(|e| anyhow::anyhow!("split tx send failed: {e}"))?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("split tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF split confirmed — tokens are in EOA, use 'Move Tokens → Proxy' for CLOB trading");
    Ok(tx_hash)
}

/// Merge YES + NO token pairs back into USDC.
/// X YES + X NO tokens -> $X USDC
///
/// Merge always runs directly from the EOA (no proxy.execute() wrapping).
/// Tokens must be in the EOA — use "Move Tokens → EOA" first if they're in
/// the proxy wallet.
pub async fn merge(config: &Config, condition_id: &str, amount_tokens: u64) -> Result<String> {
    let client = build_client(config)?;
    check_gas_balance(&client).await?;
    let ctf_addr: Address = ctf_contract(config).parse()?;

    // Merge directly from EOA — no proxy wrapping
    let merge_data = merge_positions_calldata(condition_id, amount_tokens)?;
    let merge_tx = TransactionRequest::new().to(ctf_addr).data(merge_data);

    tracing::info!(
        amount_tokens,
        condition_id,
        eoa = %format!("{:#x}", client.address()),
        "merging YES+NO tokens into USDC (EOA direct)"
    );
    let pending = client.send_transaction(merge_tx, None).await
        .map_err(|e| anyhow::anyhow!("merge tx send failed: {e}"))?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("merge tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF merge confirmed — USDC is in EOA");
    Ok(tx_hash)
}

/// Redeem winning tokens for USDC after market resolution.
///
/// Redeem always runs directly from the EOA (no proxy.execute() wrapping).
/// Tokens must be in the EOA — use "Move Tokens → EOA" first if they're in
/// the proxy wallet.
pub async fn redeem(config: &Config, condition_id: &str) -> Result<String> {
    let client = build_client(config)?;
    check_gas_balance(&client).await?;
    let ctf_addr: Address = ctf_contract(config).parse()?;

    // Redeem directly from EOA — no proxy wrapping
    let redeem_data = redeem_positions_calldata(condition_id)?;
    let redeem_tx = TransactionRequest::new().to(ctf_addr).data(redeem_data);

    tracing::info!(
        condition_id,
        eoa = %format!("{:#x}", client.address()),
        "redeeming winning tokens for USDC (EOA direct)"
    );
    let pending = client.send_transaction(redeem_tx, None).await
        .map_err(|e| anyhow::anyhow!("redeem tx send failed (market must be resolved first): {e}"))?;
    let receipt = pending.await?
        .ok_or_else(|| anyhow::anyhow!("redeem tx dropped"))?;

    let tx_hash = format!("{:#x}", receipt.transaction_hash);
    tracing::info!(tx = %tx_hash, "CTF redeem confirmed — USDC is in EOA");
    Ok(tx_hash)
}

/// Fetch ERC1155 token balance for a given token_id from the CTF contract.
///
/// Returns the balance as a `Decimal` preserving up to 6 decimal places
/// (CTF tokens share USDC's 6-decimal precision). Returning `u64` would
/// truncate fractional balances (e.g. 1.5 tokens → 1), corrupting position
/// accounting after partial fills or small splits.
///
/// Queries the balance of the correct token owner:
/// - signature_type == 1: proxy wallet (config.polymarket_address) holds the tokens
/// - signature_type == 0: EOA derived from private key holds the tokens
pub async fn balance_of(config: &Config, token_id: &str) -> Result<Decimal> {
    let provider = Provider::<Http>::try_from(config.polygon_rpc.as_str())?;
    let ctf_addr: Address = ctf_contract(config).parse()?;

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
        // val is in base units (6 decimals). Convert to Decimal to preserve fractions.
        let raw_str = val.to_string();
        let raw: u128 = raw_str.parse().unwrap_or(0);
        Ok(Decimal::from(raw) / Decimal::from(1_000_000u64))
    } else {
        Ok(Decimal::ZERO)
    }
}

/// Sync on-chain token balances into the position tracker.
/// Returns (team_a_tokens, team_b_tokens) as Decimal with 6-decimal precision.
pub async fn sync_balances(config: &Config) -> Result<(Decimal, Decimal)> {
    if !config.has_tokens() {
        bail!("token IDs not configured");
    }
    let (a, b) = tokio::try_join!(
        balance_of(config, &config.team_a_token_id),
        balance_of(config, &config.team_b_token_id),
    )?;
    Ok((a, b))
}

/// ERC-20 transfer calldata.
fn erc20_transfer_calldata(to: Address, amount_base: U256) -> Result<Bytes> {
    let selector = &keccak256(b"transfer(address,uint256)")[..4];
    let encoded = abi::encode(&[Token::Address(to), Token::Uint(amount_base)]);
    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    Ok(Bytes::from(data))
}

fn parse_token_id_u256(token_id: &str) -> Result<U256> {
    U256::from_dec_str(token_id).or_else(|_| {
        let s = token_id.strip_prefix("0x").unwrap_or(token_id);
        U256::from_str_radix(s, 16).map_err(|e| anyhow::anyhow!("{e}"))
    })
}

/// Query raw ERC-1155 balance as U256 base units for a given owner.
async fn erc1155_balance_raw(
    provider: &Provider<Http>,
    ctf_addr: Address,
    owner: Address,
    token_id: U256,
) -> Result<U256> {
    let selector = &keccak256(b"balanceOf(address,uint256)")[..4];
    let encoded = abi::encode(&[Token::Address(owner), Token::Uint(token_id)]);
    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    let call = TransactionRequest::new().to(ctf_addr).data(Bytes::from(data));
    let result = provider.call(&call.into(), None).await?;
    let decoded = abi::decode(&[ethers::abi::ParamType::Uint(256)], &result)?;
    match decoded.first() {
        Some(Token::Uint(val)) => Ok(*val),
        _ => Ok(U256::zero()),
    }
}

/// ERC-1155 safeBatchTransferFrom calldata — transfers both tokens in a single tx.
fn safe_batch_transfer_calldata(
    from: Address,
    to: Address,
    ids: &[U256],
    amounts: &[U256],
) -> Result<Bytes> {
    let selector = &keccak256(b"safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)")[..4];
    let encoded = abi::encode(&[
        Token::Address(from),
        Token::Address(to),
        Token::Array(ids.iter().copied().map(Token::Uint).collect()),
        Token::Array(amounts.iter().copied().map(Token::Uint).collect()),
        Token::Bytes(vec![]),
    ]);
    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);
    Ok(Bytes::from(data))
}

/// Move the full balance of both CTF tokens from EOA to proxy in a single batch tx.
/// Returns (tx_hash, moved_a_tokens, moved_b_tokens).
pub async fn move_tokens_to_proxy(config: &Config) -> Result<(String, Decimal, Decimal)> {
    if config.polymarket_address.is_empty() {
        anyhow::bail!("proxy wallet address not configured");
    }
    if !config.has_tokens() {
        anyhow::bail!("token IDs not configured (fetch a market first)");
    }
    let client = build_client(config)?;
    let provider = client.provider();
    let ctf_addr: Address = ctf_contract(config).parse()?;
    let eoa = client.address();
    let proxy: Address = config.polymarket_address.parse()?;

    let id_a = parse_token_id_u256(&config.team_a_token_id)?;
    let id_b = parse_token_id_u256(&config.team_b_token_id)?;

    let (bal_a, bal_b) = tokio::try_join!(
        erc1155_balance_raw(provider, ctf_addr, eoa, id_a),
        erc1155_balance_raw(provider, ctf_addr, eoa, id_b),
    )?;

    if bal_a.is_zero() && bal_b.is_zero() {
        anyhow::bail!("EOA holds no tokens to move (both balances are zero)");
    }

    let data = safe_batch_transfer_calldata(eoa, proxy, &[id_a, id_b], &[bal_a, bal_b])?;
    let receipt = client.send_transaction(
        TransactionRequest::new().to(ctf_addr).data(data), None
    ).await?.await?.ok_or_else(|| anyhow::anyhow!("batch transfer tx dropped"))?;

    let tx = format!("{:#x}", receipt.transaction_hash);
    let dec_a = Decimal::from(bal_a.as_u128()) / Decimal::from(1_000_000u64);
    let dec_b = Decimal::from(bal_b.as_u128()) / Decimal::from(1_000_000u64);
    tracing::info!(%tx, a = %dec_a, b = %dec_b, "all tokens transferred EOA → proxy (batch)");
    Ok((tx, dec_a, dec_b))
}

/// Move the full balance of both CTF tokens from proxy to EOA in a single batch tx
/// routed through proxy.execute().
pub async fn move_tokens_to_eoa(config: &Config) -> Result<(String, Decimal, Decimal)> {
    if config.polymarket_address.is_empty() || config.signature_type == 0 {
        anyhow::bail!("proxy wallet not configured (sig_type must be 1 or 2)");
    }
    if !config.has_tokens() {
        anyhow::bail!("token IDs not configured (fetch a market first)");
    }
    let client = build_client(config)?;
    let provider = client.provider();
    let ctf_addr: Address = ctf_contract(config).parse()?;
    let eoa = client.address();
    let proxy: Address = config.polymarket_address.parse()?;

    let id_a = parse_token_id_u256(&config.team_a_token_id)?;
    let id_b = parse_token_id_u256(&config.team_b_token_id)?;

    let (bal_a, bal_b) = tokio::try_join!(
        erc1155_balance_raw(provider, ctf_addr, proxy, id_a),
        erc1155_balance_raw(provider, ctf_addr, proxy, id_b),
    )?;

    if bal_a.is_zero() && bal_b.is_zero() {
        anyhow::bail!("proxy holds no tokens to move (both balances are zero)");
    }

    let inner = safe_batch_transfer_calldata(proxy, eoa, &[id_a, id_b], &[bal_a, bal_b])?;
    let (to, data) = resolve_tx(config, ctf_addr, inner)?;
    let receipt = client.send_transaction(
        TransactionRequest::new().to(to).data(data), None
    ).await?.await?.ok_or_else(|| anyhow::anyhow!("batch transfer tx dropped"))?;

    let tx = format!("{:#x}", receipt.transaction_hash);
    let dec_a = Decimal::from(bal_a.as_u128()) / Decimal::from(1_000_000u64);
    let dec_b = Decimal::from(bal_b.as_u128()) / Decimal::from(1_000_000u64);
    tracing::info!(%tx, a = %dec_a, b = %dec_b, "all tokens transferred proxy → EOA (batch)");
    Ok((tx, dec_a, dec_b))
}

/// Move USDC from EOA to proxy wallet (for trading on the CLOB).
pub async fn move_usdc_to_proxy(config: &Config, amount_usdc: u64) -> Result<String> {
    if config.polymarket_address.is_empty() {
        anyhow::bail!("proxy wallet address not configured");
    }
    let client = build_client(config)?;
    let usdc_addr: Address = USDC_CONTRACT.parse()?;
    let proxy: Address = config.polymarket_address.parse()?;
    let amount = U256::from(amount_usdc) * U256::from(1_000_000u64);

    let data = erc20_transfer_calldata(proxy, amount)?;
    let receipt = client.send_transaction(
        TransactionRequest::new().to(usdc_addr).data(data), None
    ).await?.await?.ok_or_else(|| anyhow::anyhow!("tx dropped"))?;
    tracing::info!(tx = %receipt.transaction_hash, amount_usdc, "USDC transferred EOA → proxy");
    Ok(format!("{:#x}", receipt.transaction_hash))
}

/// Move USDC from proxy wallet back to EOA (withdraw after merge/redeem).
pub async fn move_usdc_to_eoa(config: &Config, amount_usdc: u64) -> Result<String> {
    if config.polymarket_address.is_empty() || config.signature_type == 0 {
        anyhow::bail!("proxy wallet not configured (sig_type must be 1 or 2)");
    }
    let client = build_client(config)?;
    let usdc_addr: Address = USDC_CONTRACT.parse()?;
    let eoa = client.address();
    let amount = U256::from(amount_usdc) * U256::from(1_000_000u64);

    let data = erc20_transfer_calldata(eoa, amount)?;
    let (to, final_data) = resolve_tx(config, usdc_addr, data)?;
    let receipt = client.send_transaction(
        TransactionRequest::new().to(to).data(final_data), None
    ).await?.await?.ok_or_else(|| anyhow::anyhow!("tx dropped"))?;
    tracing::info!(tx = %receipt.transaction_hash, amount_usdc, "USDC transferred proxy → EOA");
    Ok(format!("{:#x}", receipt.transaction_hash))
}

/// Fetch USDC (ERC-20) balance for any Polygon address.
/// Returns balance as Decimal with 6-decimal USDC precision.
pub async fn usdc_balance(polygon_rpc: &str, address: &str) -> Result<Decimal> {
    let provider = Provider::<Http>::try_from(polygon_rpc)?;
    let usdc_addr: Address = USDC_CONTRACT.parse()?;
    let owner: Address = address.parse()
        .map_err(|e| anyhow::anyhow!("invalid address {address}: {e}"))?;

    let selector = &keccak256(b"balanceOf(address)")[..4];
    let encoded = abi::encode(&[Token::Address(owner)]);
    let mut data = selector.to_vec();
    data.extend_from_slice(&encoded);

    let call = TransactionRequest::new().to(usdc_addr).data(Bytes::from(data));
    let result = provider.call(&call.into(), None).await?;

    let decoded = abi::decode(&[ethers::abi::ParamType::Uint(256)], &result)?;
    if let Some(Token::Uint(val)) = decoded.first() {
        let raw: u128 = val.to_string().parse().unwrap_or(0);
        Ok(Decimal::from(raw) / Decimal::from(1_000_000u64))
    } else {
        Ok(Decimal::ZERO)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proxy_execute_calldata_has_correct_selector() {
        let target: Address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045".parse().unwrap();
        let inner = Bytes::from(vec![0xde, 0xad, 0xbe, 0xef]);
        let result = proxy_execute_calldata(target, inner).unwrap();
        // keccak256("execute(address,uint256,bytes)")[0..4] = 0xb61d27f6
        assert_eq!(&result[..4], &[0xb6, 0x1d, 0x27, 0xf6]);
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
