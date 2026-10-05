//! Cross-DEX atomic triangular arbitrage searcher.
//!
//! Raydium AMM v4 + Meteora DLMM  ->  3-leg cycles from wSOL  ->  single v0 tx (+ Jito tip)  ->  Jito bundle.
//!
//! Pipeline (one hot task owns all market state, no locks on the hot path):
//!
//!   Yellowstone gRPC ──► ingest/engine task ──(mpsc)──► executor task ──► Jito block engines
//!        ▲   (zero-copy parse, route eval)      (kill switch, sign, send)
//!        └── RPC snapshots (bootstrap / reconnect / new DLMM bin arrays)

use mimalloc::MiMalloc;

#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------
pub(crate) mod consts {
    use solana_sdk::pubkey::Pubkey;

    pub const RAYDIUM_AMM_V4: Pubkey =
        Pubkey::from_str_const("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8");
    pub const METEORA_DLMM: Pubkey =
        Pubkey::from_str_const("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo");
    pub const SPL_TOKEN: Pubkey =
        Pubkey::from_str_const("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
    pub const ATA_PROGRAM: Pubkey =
        Pubkey::from_str_const("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
    pub const SYSTEM_PROGRAM: Pubkey = Pubkey::from_str_const("11111111111111111111111111111111");
    pub const COMPUTE_BUDGET: Pubkey =
        Pubkey::from_str_const("ComputeBudget111111111111111111111111111111");
    pub const ALT_PROGRAM: Pubkey =
        Pubkey::from_str_const("AddressLookupTab1e1111111111111111111111111");
    pub const WSOL_MINT: Pubkey =
        Pubkey::from_str_const("So11111111111111111111111111111111111111112");

    /// Static fallback; refreshed from the block engine (`getTipAccounts`) at startup.
    pub const JITO_TIP_ACCOUNTS: [Pubkey; 8] = [
        Pubkey::from_str_const("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"),
        Pubkey::from_str_const("HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe"),
        Pubkey::from_str_const("Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY"),
        Pubkey::from_str_const("ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49"),
        Pubkey::from_str_const("DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh"),
        Pubkey::from_str_const("ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt"),
        Pubkey::from_str_const("DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL"),
        Pubkey::from_str_const("3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"),
    ];

    pub const MAX_TX_SIZE: usize = 1232;
    pub const BASE_FEE_PER_SIG: u64 = 5_000;
    pub const LAMPORTS_PER_SOL: f64 = 1_000_000_000.0;
    /// Rent-exempt minimum for a 0-byte system account; native SOL must never go below it.
    pub const RENT_EXEMPT_FLOOR: u64 = 890_880;
    /// Process exit code when the kill switch trips. systemd: RestartPreventExitStatus=42.
    pub const KILL_SWITCH_EXIT_CODE: i32 = 42;
}

// ---------------------------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------------------------
pub(crate) mod config {
    use anyhow::{Context, Result, bail};
    use serde::Deserialize;
    use std::path::{Path, PathBuf};

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    pub struct Config {
        pub network: NetworkCfg,
        pub wallet: WalletCfg,
        #[serde(default)]
        pub risk: RiskCfg,
        #[serde(default)]
        pub engine: EngineCfg,
        pub pools: Vec<PoolCfg>,
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    pub struct NetworkCfg {
        pub grpc_endpoint: String,
        #[serde(default)]
        pub grpc_x_token: Option<String>,
        #[serde(default = "d_grpc_token_env")]
        pub grpc_x_token_env: String,
        pub rpc_endpoint: String,
        #[serde(default = "d_jito")]
        pub jito_endpoints: Vec<String>,
        #[serde(default)]
        pub jito_uuid: Option<String>,
        #[serde(default = "d_jito_rps")]
        pub jito_rps_per_endpoint: f64,
        /// Stream is considered dead if no message (slots arrive every ~400ms) within this window.
        #[serde(default = "d_stall_ms")]
        pub stream_stall_timeout_ms: u64,
        /// First reconnect attempt delay. Backoff doubles per consecutive failure, capped at 2s.
        #[serde(default = "d_reconnect_ms")]
        pub reconnect_base_delay_ms: u64,
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    pub struct WalletCfg {
        pub encrypted_keypair_path: PathBuf,
        #[serde(default = "d_pass_env")]
        pub passphrase_env: String,
        #[serde(default)]
        pub passphrase_file: Option<PathBuf>,
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields, default)]
    pub struct RiskCfg {
        /// Hard floor on (native SOL + wSOL). Breach => executions stop, process exits 42.
        pub min_balance_floor_sol: f64,
        pub min_trade_sol: f64,
        pub max_trade_sol: f64,
        /// Realised profit floor after tip + fees. Enforced on-chain via leg-3 min_out.
        pub min_profit_lamports: u64,
        /// Share of net profit bid to the validator (7000 = 70%).
        pub tip_bps: u64,
        pub min_tip_lamports: u64,
        /// Max tolerated drift of final output vs simulation. Enforced on-chain via leg-3 min_out.
        pub slippage_bps: u64,
        /// Haircut on intermediate leg outputs so exact-rounding mismatches don't revert legs 2/3.
        pub intermediate_haircut_bps: u64,
        pub compute_unit_limit: u32,
        pub compute_unit_price_micro_lamports: u64,
        pub route_cooldown_ms: u64,
        pub max_opportunity_age_ms: u64,
        pub max_bundles_per_burst: usize,
        /// Simulate via RPC instead of sending bundles.
        pub dry_run: bool,
    }

    impl Default for RiskCfg {
        fn default() -> Self {
            Self {
                min_balance_floor_sol: 2.0,
                min_trade_sol: 0.05,
                max_trade_sol: 2.0,
                min_profit_lamports: 20_000,
                tip_bps: 7_000,
                min_tip_lamports: 1_000,
                slippage_bps: 50,
                intermediate_haircut_bps: 1,
                compute_unit_limit: 500_000,
                compute_unit_price_micro_lamports: 0,
                route_cooldown_ms: 400,
                max_opportunity_age_ms: 150,
                max_bundles_per_burst: 3,
                dry_run: false,
            }
        }
    }

    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields, default)]
    pub struct EngineCfg {
        pub lookup_tables: Vec<String>,
        /// Also stream transactions touching tracked pools (telemetry only; account
        /// updates already carry post-state).
        pub subscribe_transactions: bool,
        /// Liquid bin arrays tracked on each side of the active one (also max arrays per DLMM leg).
        pub dlmm_bin_arrays_per_side: usize,
        pub max_routes: usize,
        pub stats_interval_secs: u64,
        pub blockhash_refresh_ms: u64,
        pub balance_reconcile_secs: u64,
        pub worker_threads: usize,
    }

    impl Default for EngineCfg {
        fn default() -> Self {
            Self {
                lookup_tables: vec![],
                subscribe_transactions: false,
                dlmm_bin_arrays_per_side: 3,
                max_routes: 20_000,
                stats_interval_secs: 30,
                blockhash_refresh_ms: 400,
                balance_reconcile_secs: 5,
                worker_threads: 4,
            }
        }
    }

    #[derive(Debug, Deserialize, Clone, Copy, PartialEq, Eq)]
    #[serde(rename_all = "snake_case")]
    pub enum PoolKind {
        RaydiumAmmV4,
        MeteoraDlmm,
    }

    #[derive(Debug, Deserialize, Clone)]
    #[serde(deny_unknown_fields)]
    pub struct PoolCfg {
        pub address: String,
        pub kind: PoolKind,
    }

    fn d_grpc_token_env() -> String {
        "GRPC_X_TOKEN".into()
    }
    fn d_pass_env() -> String {
        "MEV_KEY_PASSPHRASE".into()
    }
    fn d_jito() -> Vec<String> {
        [
            "https://mainnet.block-engine.jito.wtf",
            "https://amsterdam.mainnet.block-engine.jito.wtf",
            "https://frankfurt.mainnet.block-engine.jito.wtf",
            "https://ny.mainnet.block-engine.jito.wtf",
            "https://tokyo.mainnet.block-engine.jito.wtf",
            "https://slc.mainnet.block-engine.jito.wtf",
        ]
        .map(String::from)
        .to_vec()
    }
    fn d_jito_rps() -> f64 {
        1.0
    }
    fn d_stall_ms() -> u64 {
        3_000
    }
    fn d_reconnect_ms() -> u64 {
        50
    }

    impl Config {
        pub fn load(path: &Path) -> Result<Self> {
            let raw = std::fs::read_to_string(path)
                .with_context(|| format!("read config {}", path.display()))?;
            let cfg: Config = toml::from_str(&raw).context("parse config")?;
            cfg.validate()?;
            Ok(cfg)
        }

        fn validate(&self) -> Result<()> {
            let r = &self.risk;
            if r.tip_bps > 10_000 || r.slippage_bps > 10_000 || r.intermediate_haircut_bps > 1_000 {
                bail!("risk.*_bps out of range");
            }
            if r.min_trade_sol <= 0.0 || r.max_trade_sol < r.min_trade_sol {
                bail!("risk.min_trade_sol / max_trade_sol invalid");
            }
            if r.min_balance_floor_sol <= 0.0 {
                bail!("risk.min_balance_floor_sol must be > 0");
            }
            if self.network.jito_rps_per_endpoint <= 0.0 {
                bail!("network.jito_rps_per_endpoint must be > 0");
            }
            if self.engine.dlmm_bin_arrays_per_side == 0 || self.engine.dlmm_bin_arrays_per_side > 6 {
                bail!("engine.dlmm_bin_arrays_per_side must be 1..=6");
            }
            if self.pools.len() < 3 {
                bail!("need at least 3 pools to form a triangle");
            }
            Ok(())
        }

        pub fn grpc_x_token(&self) -> Option<String> {
            std::env::var(&self.network.grpc_x_token_env)
                .ok()
                .filter(|s| !s.is_empty())
                .or_else(|| self.network.grpc_x_token.clone())
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Encrypted keystore: argon2id(passphrase, salt) -> XChaCha20-Poly1305(keypair[64])
// ---------------------------------------------------------------------------------------------
pub(crate) mod keystore {
    use anyhow::{Context, Result, anyhow, bail};
    use argon2::{Algorithm, Argon2, Params, Version};
    use chacha20poly1305::{
        Key, XChaCha20Poly1305, XNonce,
        aead::{Aead, KeyInit},
    };
    use solana_sdk::signature::Keypair;
    use solana_sdk::signer::Signer;
    use std::path::Path;
    use zeroize::{Zeroize, Zeroizing};

    const MAGIC: &[u8; 8] = b"MEVKEY01";
    const M_COST_KIB: u32 = 64 * 1024;
    const T_COST: u32 = 3;
    const P_COST: u32 = 1;
    // magic | m | t | p | salt[32] | nonce[24] | ct[64+16]
    const FILE_LEN: usize = 8 + 12 + 32 + 24 + 64 + 16;

    fn derive(pass: &[u8], salt: &[u8], m: u32, t: u32, p: u32) -> Result<Zeroizing<[u8; 32]>> {
        let params = Params::new(m, t, p, Some(32)).map_err(|e| anyhow!("argon2 params: {e}"))?;
        let mut out = Zeroizing::new([0u8; 32]);
        Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
            .hash_password_into(pass, salt, out.as_mut())
            .map_err(|e| anyhow!("argon2: {e}"))?;
        Ok(out)
    }

    pub fn encrypt_to_file(keypair_bytes: &[u8; 64], pass: &[u8], out: &Path) -> Result<()> {
        let mut salt = [0u8; 32];
        let mut nonce = [0u8; 24];
        rand::fill(&mut salt);
        rand::fill(&mut nonce);
        let key = derive(pass, &salt, M_COST_KIB, T_COST, P_COST)?;
        let cipher = XChaCha20Poly1305::new(&Key::from(*key));
        let ct = cipher
            .encrypt(&XNonce::from(nonce), keypair_bytes.as_slice())
            .map_err(|_| anyhow!("encrypt failed"))?;
        let mut buf = Vec::with_capacity(FILE_LEN);
        buf.extend_from_slice(MAGIC);
        buf.extend_from_slice(&M_COST_KIB.to_le_bytes());
        buf.extend_from_slice(&T_COST.to_le_bytes());
        buf.extend_from_slice(&P_COST.to_le_bytes());
        buf.extend_from_slice(&salt);
        buf.extend_from_slice(&nonce);
        buf.extend_from_slice(&ct);
        write_private(out, &buf)
    }

    #[cfg(unix)]
    fn write_private(path: &Path, data: &[u8]) -> Result<()> {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .with_context(|| format!("create {} (refusing to overwrite)", path.display()))?;
        f.write_all(data)?;
        f.sync_all()?;
        Ok(())
    }

    #[cfg(not(unix))]
    fn write_private(path: &Path, data: &[u8]) -> Result<()> {
        std::fs::write(path, data)?;
        Ok(())
    }

    pub fn decrypt_file(path: &Path, pass: &[u8]) -> Result<Keypair> {
        let buf = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(path)?.permissions().mode();
            if mode & 0o077 != 0 {
                bail!("{} is group/world accessible (mode {:o}); chmod 600 it", path.display(), mode & 0o777);
            }
        }
        if buf.len() != FILE_LEN || &buf[..8] != MAGIC {
            bail!("{} is not a MEVKEY01 file", path.display());
        }
        let rd = |o: usize| u32::from_le_bytes(buf[o..o + 4].try_into().unwrap());
        let (m, t, p) = (rd(8), rd(12), rd(16));
        let salt = &buf[20..52];
        let nonce: [u8; 24] = buf[52..76].try_into().unwrap();
        let key = derive(pass, salt, m, t, p)?;
        let cipher = XChaCha20Poly1305::new(&Key::from(*key));
        let mut pt = cipher
            .decrypt(&XNonce::from(nonce), &buf[76..])
            .map_err(|_| anyhow!("wrong passphrase or corrupted key file"))?;
        if pt.len() != 64 {
            pt.zeroize();
            bail!("bad plaintext length");
        }
        let mut secret = Zeroizing::new([0u8; 32]);
        secret.copy_from_slice(&pt[..32]);
        let expected_pub: [u8; 32] = pt[32..].try_into().unwrap();
        pt.zeroize();
        let kp = Keypair::new_from_array(*secret);
        if kp.pubkey().to_bytes() != expected_pub {
            bail!("decrypted secret does not match embedded pubkey");
        }
        Ok(kp)
    }

    /// Passphrase resolution for headless boot: env var -> configured file ->
    /// systemd credential ($CREDENTIALS_DIRECTORY/mev-key-passphrase) -> TTY prompt.
    pub fn resolve_passphrase(env_name: &str, file: Option<&Path>) -> Result<Zeroizing<String>> {
        if let Ok(v) = std::env::var(env_name) {
            if !v.is_empty() {
                return Ok(Zeroizing::new(v));
            }
        }
        let cred = std::env::var("CREDENTIALS_DIRECTORY")
            .ok()
            .map(|d| Path::new(&d).join("mev-key-passphrase"));
        for p in file.map(Path::to_path_buf).into_iter().chain(cred) {
            if p.exists() {
                let s = std::fs::read_to_string(&p).with_context(|| format!("read {}", p.display()))?;
                return Ok(Zeroizing::new(s.trim_end_matches(['\n', '\r']).to_string()));
            }
        }
        let s = rpassword::prompt_password("keystore passphrase: ")
            .context("no passphrase source (env / file / credential / tty)")?;
        Ok(Zeroizing::new(s))
    }
}

// ---------------------------------------------------------------------------------------------
// Zero-copy layout readers. All reads are little-endian at fixed offsets into the account buffer;
// buffers are length-checked once on ingestion, nothing is deserialized into intermediate structs.
// ---------------------------------------------------------------------------------------------
pub(crate) mod layout {
    use solana_sdk::pubkey::Pubkey;

    #[inline(always)]
    pub fn u8_at(d: &[u8], o: usize) -> u8 {
        d[o]
    }
    #[inline(always)]
    pub fn u16_at(d: &[u8], o: usize) -> u16 {
        u16::from_le_bytes(d[o..o + 2].try_into().unwrap())
    }
    #[inline(always)]
    pub fn u32_at(d: &[u8], o: usize) -> u32 {
        u32::from_le_bytes(d[o..o + 4].try_into().unwrap())
    }
    #[inline(always)]
    pub fn i32_at(d: &[u8], o: usize) -> i32 {
        i32::from_le_bytes(d[o..o + 4].try_into().unwrap())
    }
    #[inline(always)]
    pub fn u64_at(d: &[u8], o: usize) -> u64 {
        u64::from_le_bytes(d[o..o + 8].try_into().unwrap())
    }
    #[inline(always)]
    pub fn i64_at(d: &[u8], o: usize) -> i64 {
        i64::from_le_bytes(d[o..o + 8].try_into().unwrap())
    }
    #[inline(always)]
    pub fn u128_at(d: &[u8], o: usize) -> u128 {
        u128::from_le_bytes(d[o..o + 16].try_into().unwrap())
    }
    #[inline(always)]
    pub fn pk_at(d: &[u8], o: usize) -> Pubkey {
        Pubkey::new_from_array(d[o..o + 32].try_into().unwrap())
    }

    /// SPL Token account (165 bytes).
    pub mod spl {
        pub const LEN: usize = 165;
        pub const AMOUNT: usize = 64;
    }

    /// Raydium AMM v4 `AmmInfo` (#[repr(C, packed)], 752 bytes, no discriminator).
    pub mod ray {
        pub const LEN: usize = 752;
        pub const STATUS: usize = 0;
        pub const SWAP_FEE_NUM: usize = 176;
        pub const SWAP_FEE_DEN: usize = 184;
        pub const NEED_TAKE_PNL_COIN: usize = 192;
        pub const NEED_TAKE_PNL_PC: usize = 200;
        pub const POOL_OPEN_TIME: usize = 224;
        pub const COIN_VAULT: usize = 336;
        pub const PC_VAULT: usize = 368;
        pub const COIN_MINT: usize = 400;
        pub const PC_MINT: usize = 432;
    }

    /// Meteora DLMM (lb_clmm v0.12, bytemuck zero_copy accounts, 8-byte Anchor discriminator).
    pub mod dlmm {
        pub const LB_PAIR_LEN: usize = 904;
        pub const LB_PAIR_DISC: [u8; 8] = [33, 11, 49, 98, 181, 101, 177, 13];
        // StaticParameters @8
        pub const BASE_FACTOR: usize = 8;
        pub const FILTER_PERIOD: usize = 10;
        pub const DECAY_PERIOD: usize = 12;
        pub const REDUCTION_FACTOR: usize = 14;
        pub const VARIABLE_FEE_CONTROL: usize = 16;
        pub const MAX_VOLATILITY_ACC: usize = 20;
        pub const BASE_FEE_POWER_FACTOR: usize = 34;
        pub const FUNCTION_TYPE: usize = 35;
        pub const COLLECT_FEE_MODE: usize = 36;
        // VariableParameters @40
        pub const VOLATILITY_ACC: usize = 40;
        pub const VOLATILITY_REF: usize = 44;
        pub const INDEX_REF: usize = 48;
        pub const LAST_UPDATE_TS: usize = 56;
        pub const PAIR_TYPE: usize = 75;
        pub const ACTIVE_ID: usize = 76;
        pub const BIN_STEP: usize = 80;
        pub const STATUS: usize = 82;
        pub const ACTIVATION_TYPE: usize = 86;
        pub const TOKEN_X_MINT: usize = 88;
        pub const TOKEN_Y_MINT: usize = 120;
        pub const RESERVE_X: usize = 152;
        pub const RESERVE_Y: usize = 184;
        pub const REWARD0_MINT: usize = 264;
        pub const REWARD1_MINT: usize = 264 + 144;
        pub const ORACLE: usize = 552;
        pub const BITMAP: usize = 584;
        pub const ACTIVATION_POINT: usize = 816;
        pub const TOKEN_X_PROGRAM_FLAG: usize = 880;
        pub const TOKEN_Y_PROGRAM_FLAG: usize = 881;

        pub const BIN_ARRAY_LEN: usize = 10_136;
        pub const BIN_ARRAY_DISC: [u8; 8] = [92, 142, 92, 220, 5, 148, 70, 181];
        pub const BIN_ARRAY_INDEX: usize = 8;
        pub const BIN_ARRAY_LB_PAIR: usize = 24;
        pub const BINS: usize = 56;
        pub const BIN_SIZE: usize = 144;
        // Bin fields
        pub const BIN_AMOUNT_X: usize = 0;
        pub const BIN_AMOUNT_Y: usize = 8;
        pub const BIN_PRICE: usize = 16;
        pub const BIN_OPEN_ORDER: usize = 112;
        pub const BIN_PROCESSED_REMAINING: usize = 128;
        pub const BIN_LO_ASK_SIDE: usize = 140;

        pub const SWAP_DISC: [u8; 8] = [248, 198, 158, 145, 225, 117, 135, 200];
    }
}

// ---------------------------------------------------------------------------------------------
// AMM math. Bit-exact ports of the on-chain integer math (rounding included).
// ---------------------------------------------------------------------------------------------
pub(crate) mod math {
    /// Raydium AMM v4 `swap_base_in_v2`: fee = ceil(in * num / den); x*y=k on vault reserves net of
    /// pending PnL.
    #[inline]
    pub fn raydium_quote(
        reserve_in: u64,
        reserve_out: u64,
        fee_num: u64,
        fee_den: u64,
        amount_in: u64,
    ) -> Option<u64> {
        if amount_in == 0 || fee_den == 0 || reserve_in == 0 || reserve_out == 0 {
            return None;
        }
        let a = amount_in as u128;
        let fee = (a * fee_num as u128).div_ceil(fee_den as u128);
        let net = a.checked_sub(fee)?;
        let out = (reserve_out as u128 * net) / (reserve_in as u128 + net);
        if out == 0 { None } else { u64::try_from(out).ok() }
    }

    pub mod dlmm {
        pub const ONE: u128 = 1u128 << 64;
        pub const MAX_BIN_PER_ARRAY: i32 = 70;
        pub const MIN_BIN_ID: i32 = -443_636;
        pub const MAX_BIN_ID: i32 = 443_636;
        pub const MAX_FEE_RATE: u128 = 100_000_000;
        pub const FEE_PRECISION: u128 = 1_000_000_000;
        pub const BITMAP_HALF: i32 = 512;

        /// Q64.64 exponentiation, identical bit pattern to lb_clmm `u64x64_math::pow`.
        pub fn pow(base: u128, exp: i32) -> Option<u128> {
            if exp == 0 {
                return Some(ONE);
            }
            let mut invert = exp < 0;
            let e = exp.unsigned_abs();
            if e >= 0x80000 {
                return None;
            }
            let mut sq = base;
            let mut result = ONE;
            if sq >= result {
                sq = u128::MAX.checked_div(sq)?;
                invert = !invert;
            }
            for bit in 0..19u32 {
                if bit > 0 {
                    sq = sq.checked_mul(sq)? >> 64;
                }
                if e & (1 << bit) != 0 {
                    result = result.checked_mul(sq)? >> 64;
                }
            }
            if result == 0 {
                return None;
            }
            if invert {
                result = u128::MAX.checked_div(result)?;
            }
            Some(result)
        }

        pub fn price_from_id(id: i32, bin_step: u16) -> Option<u128> {
            let bps = (u128::from(bin_step) << 64) / 10_000;
            pow(ONE + bps, id)
        }

        /// (x * y) >> 64 with 192-bit intermediate. `y` is a token amount.
        #[inline(always)]
        pub fn mul_shr(x: u128, y: u64, round_up: bool) -> Option<u64> {
            let y = y as u128;
            let lo = (x & u64::MAX as u128) * y;
            let hi = (x >> 64) * y;
            let mut q = hi.checked_add(lo >> 64)?;
            if round_up && (lo as u64) != 0 {
                q = q.checked_add(1)?;
            }
            u64::try_from(q).ok()
        }

        /// (x << 64) / y
        #[inline(always)]
        pub fn shl_div(x: u64, y: u128, round_up: bool) -> Option<u64> {
            if y == 0 {
                return None;
            }
            let n = (x as u128) << 64;
            let mut q = n / y;
            if round_up && n % y != 0 {
                q += 1;
            }
            u64::try_from(q).ok()
        }

        #[inline(always)]
        pub fn amount_in_for_out(out: u64, price: u128, swap_for_y: bool) -> Option<u64> {
            if swap_for_y { shl_div(out, price, true) } else { mul_shr(price, out, true) }
        }

        #[inline(always)]
        pub fn amount_out_for_in(inp: u64, price: u128, swap_for_y: bool) -> Option<u64> {
            if swap_for_y { mul_shr(price, inp, false) } else { shl_div(inp, price, false) }
        }

        #[inline(always)]
        pub fn bin_array_index(bin_id: i32) -> i32 {
            let idx = bin_id / MAX_BIN_PER_ARRAY;
            if bin_id < 0 && bin_id % MAX_BIN_PER_ARRAY != 0 { idx - 1 } else { idx }
        }

        #[inline(always)]
        pub fn array_bounds(idx: i32) -> (i32, i32) {
            let lo = idx * MAX_BIN_PER_ARRAY;
            (lo, lo + MAX_BIN_PER_ARRAY - 1)
        }

        /// Next bin array (inclusive of `start`) flagged as liquid in the internal 1024-bit bitmap.
        /// None => outside the internal bitmap (needs bitmap extension; unsupported) or no liquidity.
        pub fn next_liquid_array(bitmap: &[u64; 16], start: i32, swap_for_y: bool) -> Option<i32> {
            let mut idx = start;
            let step = if swap_for_y { -1 } else { 1 };
            while (-BITMAP_HALF..BITMAP_HALF).contains(&idx) {
                let off = (idx + BITMAP_HALF) as usize;
                let word = bitmap[off >> 6];
                if word == 0 {
                    // skip the whole empty word
                    idx = if swap_for_y {
                        ((off & !63) as i32 - 1) - BITMAP_HALF
                    } else {
                        ((off | 63) as i32 + 1) - BITMAP_HALF
                    };
                    continue;
                }
                if (word >> (off & 63)) & 1 == 1 {
                    return Some(idx);
                }
                idx += step;
            }
            None
        }

        #[derive(Clone, Copy, Debug, Default)]
        pub struct StaticParams {
            pub base_factor: u16,
            pub filter_period: u16,
            pub decay_period: u16,
            pub reduction_factor: u16,
            pub variable_fee_control: u32,
            pub max_volatility_accumulator: u32,
            pub base_fee_power_factor: u8,
            pub function_type: u8,
            pub collect_fee_mode: u8,
        }

        #[derive(Clone, Copy, Debug, Default)]
        pub struct VarParams {
            pub volatility_accumulator: u32,
            pub volatility_reference: u32,
            pub index_reference: i32,
            pub last_update_timestamp: i64,
        }

        impl VarParams {
            pub fn update_references(&mut self, sp: &StaticParams, active_id: i32, now: i64) {
                let elapsed = now - self.last_update_timestamp;
                if elapsed >= sp.filter_period as i64 {
                    self.index_reference = active_id;
                    self.volatility_reference = if elapsed < sp.decay_period as i64 {
                        ((self.volatility_accumulator as u64 * sp.reduction_factor as u64) / 10_000) as u32
                    } else {
                        0
                    };
                }
            }

            pub fn update_volatility_accumulator(&mut self, sp: &StaticParams, active_id: i32) {
                let delta = (self.index_reference as i64 - active_id as i64).unsigned_abs();
                let va = self.volatility_reference as u64 + delta * 10_000;
                self.volatility_accumulator = va.min(sp.max_volatility_accumulator as u64) as u32;
            }
        }

        #[inline]
        pub fn total_fee_rate(sp: &StaticParams, va: u32, bin_step: u16) -> u128 {
            let base = u128::from(sp.base_factor)
                * u128::from(bin_step)
                * 10
                * 10u128.pow(sp.base_fee_power_factor as u32);
            let var = if sp.variable_fee_control > 0 {
                let x = u128::from(va) * u128::from(bin_step);
                (u128::from(sp.variable_fee_control) * x * x).div_ceil(100_000_000_000)
            } else {
                0
            };
            (base + var).min(MAX_FEE_RATE)
        }

        /// Fee to add on top of a fee-exclusive amount (ceil).
        #[inline]
        pub fn fee_on_top(amount: u64, rate: u128) -> u64 {
            let den = FEE_PRECISION - rate;
            ((amount as u128 * rate).div_ceil(den)) as u64
        }

        /// Fee contained in a fee-inclusive amount (ceil).
        #[inline]
        pub fn fee_included(amount: u64, rate: u128) -> u64 {
            ((amount as u128 * rate).div_ceil(FEE_PRECISION)) as u64
        }

        /// Read-only view over one 144-byte `Bin` inside a BinArray buffer.
        #[derive(Clone, Copy)]
        pub struct BinView<'a>(pub &'a [u8]);

        impl BinView<'_> {
            #[inline(always)]
            pub fn amount_x(&self) -> u64 {
                u64_at(self.0, BIN_AMOUNT_X)
            }
            #[inline(always)]
            pub fn amount_y(&self) -> u64 {
                u64_at(self.0, BIN_AMOUNT_Y)
            }
            #[inline(always)]
            pub fn price(&self) -> u128 {
                u128_at(self.0, BIN_PRICE)
            }
            /// (open_order_amount, processed_order_remaining_amount) matching the swap side.
            #[inline(always)]
            pub fn limit_orders(&self, swap_for_y: bool) -> (u64, u64) {
                let ask = u8_at(self.0, BIN_LO_ASK_SIDE) != 0;
                if swap_for_y != ask {
                    (u64_at(self.0, BIN_OPEN_ORDER), u64_at(self.0, BIN_PROCESSED_REMAINING))
                } else {
                    (0, 0)
                }
            }
            #[inline(always)]
            pub fn mm_out(&self, swap_for_y: bool) -> u64 {
                if swap_for_y { self.amount_y() } else { self.amount_x() }
            }
            #[inline(always)]
            pub fn max_out(&self, swap_for_y: bool, lo: bool) -> u64 {
                let mm = self.mm_out(swap_for_y);
                if !lo {
                    return mm;
                }
                let (o, p) = self.limit_orders(swap_for_y);
                mm.saturating_add(o).saturating_add(p)
            }
        }

        use crate::layout::dlmm::*;
        use crate::layout::*;

        /// (amount_in_used, amount_left, out)
        #[inline(always)]
        fn fill_layer(price: u128, amount: u64, max_out: u64, sfy: bool) -> Option<(u64, u64, u64)> {
            if max_out == 0 {
                return Some((0, amount, 0));
            }
            let max_in = amount_in_for_out(max_out, price, sfy)?;
            if amount >= max_in {
                Some((max_in, amount - max_in, max_out))
            } else {
                Some((amount, 0, amount_out_for_in(amount, price, sfy)?))
            }
        }

        /// Exact-in fill of one bin across MM liquidity, then processed and open limit orders.
        /// Mirrors `swap_exact_in_quote_at_bin` of the DLMM SDK. Returns (in_consumed, out).
        #[inline]
        pub fn swap_in_bin(
            bin: BinView<'_>,
            price: u128,
            rate: u128,
            in_amount: u64,
            sfy: bool,
            lo: bool,
            fee_on_input: bool,
        ) -> Option<(u64, u64)> {
            let mut excl = in_amount;
            if fee_on_input {
                excl = in_amount.checked_sub(fee_included(in_amount, rate))?;
            }
            let (mut used, mut left, mut out) = fill_layer(price, excl, bin.mm_out(sfy), sfy)?;
            if lo && left > 0 {
                let (open, processed) = bin.limit_orders(sfy);
                let (u2, l2, o2) = fill_layer(price, left, processed, sfy)?;
                used += u2;
                out += o2;
                left = l2;
                if left > 0 {
                    let (u3, l3, o3) = fill_layer(price, left, open, sfy)?;
                    used += u3;
                    out += o3;
                    left = l3;
                }
            }
            let _ = used;
            let mut included = in_amount;
            if left > 0 {
                excl -= left;
                included = if fee_on_input { excl + fee_on_top(excl, rate) } else { excl };
            }
            if !fee_on_input {
                out -= fee_included(out, rate);
            }
            Some((included, out))
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Pool state
// ---------------------------------------------------------------------------------------------
pub(crate) mod pools {
    use crate::consts::*;
    use crate::layout::{self, *};
    use crate::math::dlmm::{self as dm, BinView, StaticParams, VarParams};
    use solana_sdk::pubkey::Pubkey;
    use std::collections::HashMap;

    #[derive(Debug)]
    pub struct RayPool {
        pub addr: Pubkey,
        pub coin_mint: Pubkey,
        pub pc_mint: Pubkey,
        pub coin_vault: Pubkey,
        pub pc_vault: Pubkey,
        pub status: u64,
        pub open_time: u64,
        pub fee_num: u64,
        pub fee_den: u64,
        pub pnl_coin: u64,
        pub pnl_pc: u64,
        pub coin_amt: u64,
        pub pc_amt: u64,
        pub coin_sig: u64,
        pub pc_sig: u64,
        pub has_coin: bool,
        pub has_pc: bool,
    }

    impl RayPool {
        pub fn parse(addr: Pubkey, d: &[u8]) -> Option<Self> {
            if d.len() != layout::ray::LEN {
                return None;
            }
            let mut p = RayPool {
                addr,
                coin_mint: pk_at(d, layout::ray::COIN_MINT),
                pc_mint: pk_at(d, layout::ray::PC_MINT),
                coin_vault: pk_at(d, layout::ray::COIN_VAULT),
                pc_vault: pk_at(d, layout::ray::PC_VAULT),
                status: 0,
                open_time: 0,
                fee_num: 0,
                fee_den: 0,
                pnl_coin: 0,
                pnl_pc: 0,
                coin_amt: 0,
                pc_amt: 0,
                coin_sig: 0,
                pc_sig: 0,
                has_coin: false,
                has_pc: false,
            };
            p.update_amm(d);
            Some(p)
        }

        #[inline]
        pub fn update_amm(&mut self, d: &[u8]) {
            use layout::ray::*;
            self.status = u64_at(d, STATUS);
            self.open_time = u64_at(d, POOL_OPEN_TIME);
            self.fee_num = u64_at(d, SWAP_FEE_NUM);
            self.fee_den = u64_at(d, SWAP_FEE_DEN);
            self.pnl_coin = u64_at(d, NEED_TAKE_PNL_COIN);
            self.pnl_pc = u64_at(d, NEED_TAKE_PNL_PC);
        }

        #[inline]
        fn swappable(&self, now: i64) -> bool {
            match self.status {
                1 | 6 => true,
                7 => now as u64 >= self.open_time,
                _ => false,
            }
        }

        #[inline]
        pub fn ready(&self) -> bool {
            self.has_coin && self.has_pc && (self.coin_sig == self.pc_sig || self.coin_sig == 0 || self.pc_sig == 0)
        }

        #[inline]
        pub fn quote(&self, amount_in: u64, coin_to_pc: bool, now: i64) -> Option<u64> {
            if !self.swappable(now) {
                return None;
            }
            let coin = self.coin_amt.checked_sub(self.pnl_coin)?;
            let pc = self.pc_amt.checked_sub(self.pnl_pc)?;
            let (rin, rout) = if coin_to_pc { (coin, pc) } else { (pc, coin) };
            crate::math::raydium_quote(rin, rout, self.fee_num, self.fee_den, amount_in)
        }
    }

    pub struct BinArraySlot {
        pub key: Pubkey,
        /// Raw account buffer, owned straight out of the decoded gRPC frame (no re-copy).
        pub data: Option<Vec<u8>>,
    }

    pub struct DlmmPool {
        pub addr: Pubkey,
        pub mint_x: Pubkey,
        pub mint_y: Pubkey,
        pub reserve_x: Pubkey,
        pub reserve_y: Pubkey,
        pub oracle: Pubkey,
        pub sp: StaticParams,
        pub vp: VarParams,
        pub active_id: i32,
        pub bin_step: u16,
        pub status: u8,
        pub pair_type: u8,
        pub activation_type: u8,
        pub activation_point: u64,
        pub support_lo: bool,
        pub bitmap: [u64; 16],
        pub arrays: HashMap<i32, BinArraySlot>,
    }

    pub fn bin_array_pda(lb_pair: &Pubkey, idx: i32) -> Pubkey {
        Pubkey::find_program_address(
            &[b"bin_array", lb_pair.as_ref(), &(idx as i64).to_le_bytes()],
            &METEORA_DLMM,
        )
        .0
    }

    impl DlmmPool {
        /// Returns None for unsupported pairs (Token-2022 legs, bad layout).
        pub fn parse(addr: Pubkey, d: &[u8]) -> Option<Self> {
            use layout::dlmm::*;
            if d.len() != LB_PAIR_LEN || d[..8] != LB_PAIR_DISC {
                return None;
            }
            if u8_at(d, TOKEN_X_PROGRAM_FLAG) != 0 || u8_at(d, TOKEN_Y_PROGRAM_FLAG) != 0 {
                return None;
            }
            let mut p = DlmmPool {
                addr,
                mint_x: pk_at(d, TOKEN_X_MINT),
                mint_y: pk_at(d, TOKEN_Y_MINT),
                reserve_x: pk_at(d, RESERVE_X),
                reserve_y: pk_at(d, RESERVE_Y),
                oracle: pk_at(d, ORACLE),
                sp: StaticParams::default(),
                vp: VarParams::default(),
                active_id: 0,
                bin_step: 0,
                status: 0,
                pair_type: 0,
                activation_type: 0,
                activation_point: 0,
                support_lo: false,
                bitmap: [0; 16],
                arrays: HashMap::new(),
            };
            p.update_pair(d);
            Some(p)
        }

        #[inline]
        pub fn update_pair(&mut self, d: &[u8]) {
            use layout::dlmm::*;
            self.sp = StaticParams {
                base_factor: u16_at(d, BASE_FACTOR),
                filter_period: u16_at(d, FILTER_PERIOD),
                decay_period: u16_at(d, DECAY_PERIOD),
                reduction_factor: u16_at(d, REDUCTION_FACTOR),
                variable_fee_control: u32_at(d, VARIABLE_FEE_CONTROL),
                max_volatility_accumulator: u32_at(d, MAX_VOLATILITY_ACC),
                base_fee_power_factor: u8_at(d, BASE_FEE_POWER_FACTOR),
                function_type: u8_at(d, FUNCTION_TYPE),
                collect_fee_mode: u8_at(d, COLLECT_FEE_MODE),
            };
            self.vp = VarParams {
                volatility_accumulator: u32_at(d, VOLATILITY_ACC),
                volatility_reference: u32_at(d, VOLATILITY_REF),
                index_reference: i32_at(d, INDEX_REF),
                last_update_timestamp: i64_at(d, LAST_UPDATE_TS),
            };
            self.pair_type = u8_at(d, PAIR_TYPE);
            self.active_id = i32_at(d, ACTIVE_ID);
            self.bin_step = u16_at(d, BIN_STEP);
            self.status = u8_at(d, STATUS);
            self.activation_type = u8_at(d, ACTIVATION_TYPE);
            self.activation_point = u64_at(d, ACTIVATION_POINT);
            for (i, w) in self.bitmap.iter_mut().enumerate() {
                *w = u64_at(d, BITMAP + i * 8);
            }
            self.support_lo = match self.sp.function_type {
                2 => true,
                0 => pk_at(d, REWARD0_MINT) == Pubkey::default() && pk_at(d, REWARD1_MINT) == Pubkey::default(),
                _ => false,
            };
        }

        #[inline]
        fn fee_on_input(&self, sfy: bool) -> bool {
            match self.sp.collect_fee_mode {
                1 => !sfy,
                _ => true,
            }
        }

        #[inline]
        fn tradable(&self, now: i64, slot: u64) -> bool {
            if self.status != 0 {
                return false;
            }
            if self.pair_type == 1 || self.pair_type == 2 {
                let point = if self.activation_type == 0 { slot } else { now as u64 };
                return point >= self.activation_point;
            }
            true
        }

        /// Liquid bin-array indices in swap order starting at the active array (bitmap-driven,
        /// identical to the on-chain traversal).
        pub fn liquid_arrays(&self, sfy: bool, take: usize, out: &mut Vec<i32>) {
            let step = if sfy { -1 } else { 1 };
            let mut start = dm::bin_array_index(self.active_id);
            while out.len() < take {
                match dm::next_liquid_array(&self.bitmap, start, sfy) {
                    Some(i) => {
                        out.push(i);
                        start = i + step;
                    }
                    None => break,
                }
            }
        }

        #[inline]
        pub fn ready(&self) -> bool {
            let idx = dm::bin_array_index(self.active_id);
            match dm::next_liquid_array(&self.bitmap, idx, true)
                .or_else(|| dm::next_liquid_array(&self.bitmap, idx, false))
            {
                Some(i) => self.arrays.get(&i).is_some_and(|s| s.data.is_some()),
                None => false,
            }
        }

        /// Exact-in quote. Mirrors `quote_exact_in` (limit orders, fee mode, volatility updates per
        /// crossed bin). `used` receives the bin-array indices the swap touches, in order.
        pub fn quote(
            &self,
            amount_in: u64,
            sfy: bool,
            now: i64,
            slot: u64,
            max_arrays: usize,
            mut used: Option<&mut Vec<i32>>,
        ) -> Option<u64> {
            if amount_in == 0 || !self.tradable(now, slot) {
                return None;
            }
            let mut vp = self.vp;
            vp.update_references(&self.sp, self.active_id, now);
            let fee_in = self.fee_on_input(sfy);
            let lo = self.support_lo;
            let step = if sfy { -1 } else { 1 };
            let mut active = self.active_id;
            let mut left = amount_in;
            let mut out: u64 = 0;
            let mut n_arrays = 0usize;

            while left > 0 {
                let arr_idx = dm::next_liquid_array(&self.bitmap, dm::bin_array_index(active), sfy)?;
                n_arrays += 1;
                if n_arrays > max_arrays {
                    return None;
                }
                let data = self.arrays.get(&arr_idx)?.data.as_deref()?;
                if let Some(u) = used.as_deref_mut() {
                    u.push(arr_idx);
                }
                let (lower, upper) = dm::array_bounds(arr_idx);
                if dm::bin_array_index(active) != arr_idx {
                    active = if sfy { upper } else { lower };
                }
                while (lower..=upper).contains(&active) && left > 0 {
                    let off = layout::dlmm::BINS + (active - lower) as usize * layout::dlmm::BIN_SIZE;
                    let bin = BinView(&data[off..off + layout::dlmm::BIN_SIZE]);
                    if bin.max_out(sfy, lo) > 0 {
                        vp.update_volatility_accumulator(&self.sp, active);
                        let rate = dm::total_fee_rate(&self.sp, vp.volatility_accumulator, self.bin_step);
                        let mut price = bin.price();
                        if price == 0 {
                            price = dm::price_from_id(active, self.bin_step)?;
                        }
                        let (used_in, o) = dm::swap_in_bin(bin, price, rate, left, sfy, lo, fee_in)?;
                        if used_in > 0 {
                            left = left.checked_sub(used_in)?;
                            out = out.checked_add(o)?;
                        }
                    }
                    if left > 0 {
                        active += step;
                        if !(dm::MIN_BIN_ID..=dm::MAX_BIN_ID).contains(&active) {
                            return None;
                        }
                    }
                }
            }
            if out == 0 { None } else { Some(out) }
        }
    }

    pub enum Pool {
        Ray(RayPool),
        Dlmm(DlmmPool),
    }

    impl Pool {
        pub fn addr(&self) -> Pubkey {
            match self {
                Pool::Ray(p) => p.addr,
                Pool::Dlmm(p) => p.addr,
            }
        }
        /// (mint_a, mint_b). Direction flag `true` means a -> b (ray: coin->pc, dlmm: x->y).
        pub fn mints(&self) -> (Pubkey, Pubkey) {
            match self {
                Pool::Ray(p) => (p.coin_mint, p.pc_mint),
                Pool::Dlmm(p) => (p.mint_x, p.mint_y),
            }
        }
        pub fn ready(&self) -> bool {
            match self {
                Pool::Ray(p) => p.ready(),
                Pool::Dlmm(p) => p.ready(),
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Instruction builders (raw; no SDK helper crates on the hot path)
// ---------------------------------------------------------------------------------------------
pub(crate) mod ixs {
    use crate::consts::*;
    use crate::pools::{DlmmPool, RayPool};
    use solana_sdk::instruction::{AccountMeta, Instruction};
    use solana_sdk::pubkey::Pubkey;
    use std::sync::OnceLock;

    pub fn raydium_authority() -> Pubkey {
        static A: OnceLock<Pubkey> = OnceLock::new();
        *A.get_or_init(|| Pubkey::find_program_address(&[b"amm authority"], &RAYDIUM_AMM_V4).0)
    }

    pub fn dlmm_event_authority() -> Pubkey {
        static A: OnceLock<Pubkey> = OnceLock::new();
        *A.get_or_init(|| Pubkey::find_program_address(&[b"__event_authority"], &METEORA_DLMM).0)
    }

    pub fn ata(owner: &Pubkey, mint: &Pubkey) -> Pubkey {
        Pubkey::find_program_address(&[owner.as_ref(), SPL_TOKEN.as_ref(), mint.as_ref()], &ATA_PROGRAM).0
    }

    /// Raydium AMM v4 `SwapBaseInV2` (tag 16): orderbook-free, 8 accounts.
    pub fn raydium_swap(p: &RayPool, src: Pubkey, dst: Pubkey, owner: Pubkey, amount_in: u64, min_out: u64) -> Instruction {
        let mut data = Vec::with_capacity(17);
        data.push(16u8);
        data.extend_from_slice(&amount_in.to_le_bytes());
        data.extend_from_slice(&min_out.to_le_bytes());
        Instruction {
            program_id: RAYDIUM_AMM_V4,
            accounts: vec![
                AccountMeta::new_readonly(SPL_TOKEN, false),
                AccountMeta::new(p.addr, false),
                AccountMeta::new_readonly(raydium_authority(), false),
                AccountMeta::new(p.coin_vault, false),
                AccountMeta::new(p.pc_vault, false),
                AccountMeta::new(src, false),
                AccountMeta::new(dst, false),
                AccountMeta::new_readonly(owner, true),
            ],
            data,
        }
    }

    /// Meteora DLMM `swap` (exact in). Optional accounts (bitmap ext, host fee) => program id.
    pub fn dlmm_swap(
        p: &DlmmPool,
        user_in: Pubkey,
        user_out: Pubkey,
        owner: Pubkey,
        bin_arrays: &[Pubkey],
        amount_in: u64,
        min_out: u64,
    ) -> Instruction {
        let mut data = Vec::with_capacity(24);
        data.extend_from_slice(&crate::layout::dlmm::SWAP_DISC);
        data.extend_from_slice(&amount_in.to_le_bytes());
        data.extend_from_slice(&min_out.to_le_bytes());
        let mut accounts = Vec::with_capacity(15 + bin_arrays.len());
        accounts.extend_from_slice(&[
            AccountMeta::new(p.addr, false),
            AccountMeta::new_readonly(METEORA_DLMM, false),
            AccountMeta::new(p.reserve_x, false),
            AccountMeta::new(p.reserve_y, false),
            AccountMeta::new(user_in, false),
            AccountMeta::new(user_out, false),
            AccountMeta::new_readonly(p.mint_x, false),
            AccountMeta::new_readonly(p.mint_y, false),
            AccountMeta::new(p.oracle, false),
            AccountMeta::new_readonly(METEORA_DLMM, false),
            AccountMeta::new_readonly(owner, true),
            AccountMeta::new_readonly(SPL_TOKEN, false),
            AccountMeta::new_readonly(SPL_TOKEN, false),
            AccountMeta::new_readonly(dlmm_event_authority(), false),
            AccountMeta::new_readonly(METEORA_DLMM, false),
        ]);
        accounts.extend(bin_arrays.iter().map(|k| AccountMeta::new(*k, false)));
        Instruction { program_id: METEORA_DLMM, accounts, data }
    }

    pub fn cu_limit(units: u32) -> Instruction {
        let mut data = vec![2u8];
        data.extend_from_slice(&units.to_le_bytes());
        Instruction { program_id: COMPUTE_BUDGET, accounts: vec![], data }
    }

    pub fn cu_price(micro_lamports: u64) -> Instruction {
        let mut data = vec![3u8];
        data.extend_from_slice(&micro_lamports.to_le_bytes());
        Instruction { program_id: COMPUTE_BUDGET, accounts: vec![], data }
    }

    pub fn transfer(from: Pubkey, to: Pubkey, lamports: u64) -> Instruction {
        let mut data = Vec::with_capacity(12);
        data.extend_from_slice(&2u32.to_le_bytes());
        data.extend_from_slice(&lamports.to_le_bytes());
        Instruction {
            program_id: SYSTEM_PROGRAM,
            accounts: vec![AccountMeta::new(from, true), AccountMeta::new(to, false)],
            data,
        }
    }

    pub fn create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction {
        Instruction {
            program_id: ATA_PROGRAM,
            accounts: vec![
                AccountMeta::new(payer, true),
                AccountMeta::new(ata(&owner, &mint), false),
                AccountMeta::new_readonly(owner, false),
                AccountMeta::new_readonly(mint, false),
                AccountMeta::new_readonly(SYSTEM_PROGRAM, false),
                AccountMeta::new_readonly(SPL_TOKEN, false),
            ],
            data: vec![1],
        }
    }

    pub fn sync_native(account: Pubkey) -> Instruction {
        Instruction { program_id: SPL_TOKEN, accounts: vec![AccountMeta::new(account, false)], data: vec![17] }
    }

    pub fn alt_create(authority: Pubkey, payer: Pubkey, recent_slot: u64) -> (Instruction, Pubkey) {
        let (table, bump) =
            Pubkey::find_program_address(&[authority.as_ref(), &recent_slot.to_le_bytes()], &ALT_PROGRAM);
        let mut data = Vec::with_capacity(13);
        data.extend_from_slice(&0u32.to_le_bytes());
        data.extend_from_slice(&recent_slot.to_le_bytes());
        data.push(bump);
        let ix = Instruction {
            program_id: ALT_PROGRAM,
            accounts: vec![
                AccountMeta::new(table, false),
                AccountMeta::new_readonly(authority, true),
                AccountMeta::new(payer, true),
                AccountMeta::new_readonly(SYSTEM_PROGRAM, false),
            ],
            data,
        };
        (ix, table)
    }

    pub fn alt_extend(table: Pubkey, authority: Pubkey, payer: Pubkey, keys: &[Pubkey]) -> Instruction {
        let mut data = Vec::with_capacity(12 + 32 * keys.len());
        data.extend_from_slice(&2u32.to_le_bytes());
        data.extend_from_slice(&(keys.len() as u64).to_le_bytes());
        for k in keys {
            data.extend_from_slice(k.as_ref());
        }
        Instruction {
            program_id: ALT_PROGRAM,
            accounts: vec![
                AccountMeta::new(table, false),
                AccountMeta::new_readonly(authority, true),
                AccountMeta::new(payer, true),
                AccountMeta::new_readonly(SYSTEM_PROGRAM, false),
            ],
            data,
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Minimal JSON-RPC client (no solana-client: smaller tree, no version lockstep with Agave)
// ---------------------------------------------------------------------------------------------
pub(crate) mod rpc {
    use anyhow::{Context, Result, anyhow, bail};
    use base64::Engine;
    use serde_json::{Value, json};
    use solana_sdk::hash::Hash;
    use solana_sdk::pubkey::Pubkey;
    use std::str::FromStr;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::Duration;

    pub struct RpcAccount {
        pub lamports: u64,
        pub owner: Pubkey,
        pub data: Vec<u8>,
    }

    pub struct Rpc {
        http: reqwest::Client,
        url: String,
        id: AtomicU64,
    }

    impl Rpc {
        pub fn new(url: &str) -> Result<Self> {
            let http = reqwest::Client::builder()
                .tcp_nodelay(true)
                .timeout(Duration::from_secs(8))
                .connect_timeout(Duration::from_secs(3))
                .pool_max_idle_per_host(8)
                .build()?;
            Ok(Self { http, url: url.to_string(), id: AtomicU64::new(1) })
        }

        pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
            let id = self.id.fetch_add(1, Ordering::Relaxed);
            let body = json!({"jsonrpc":"2.0","id":id,"method":method,"params":params});
            let resp: Value = self
                .http
                .post(&self.url)
                .json(&body)
                .send()
                .await
                .with_context(|| format!("rpc {method} send"))?
                .error_for_status()?
                .json()
                .await
                .with_context(|| format!("rpc {method} decode"))?;
            if let Some(err) = resp.get("error") {
                bail!("rpc {method}: {err}");
            }
            resp.get("result").cloned().ok_or_else(|| anyhow!("rpc {method}: no result"))
        }

        /// Returns (context slot, accounts) for any number of keys (chunks of 100).
        pub async fn get_multiple_accounts(
            &self,
            keys: &[Pubkey],
            commitment: &str,
        ) -> Result<(u64, Vec<Option<RpcAccount>>)> {
            let mut out = Vec::with_capacity(keys.len());
            let mut min_slot = u64::MAX;
            for chunk in keys.chunks(100) {
                let ks: Vec<String> = chunk.iter().map(|k| k.to_string()).collect();
                let r = self
                    .call("getMultipleAccounts", json!([ks, {"encoding":"base64","commitment":commitment}]))
                    .await?;
                min_slot = min_slot.min(r["context"]["slot"].as_u64().unwrap_or(0));
                let vals = r["value"].as_array().ok_or_else(|| anyhow!("getMultipleAccounts: bad value"))?;
                for v in vals {
                    out.push(parse_account(v)?);
                }
            }
            Ok((if min_slot == u64::MAX { 0 } else { min_slot }, out))
        }

        pub async fn latest_blockhash(&self) -> Result<(Hash, u64)> {
            let r = self.call("getLatestBlockhash", json!([{"commitment":"confirmed"}])).await?;
            let h = r["value"]["blockhash"].as_str().ok_or_else(|| anyhow!("no blockhash"))?;
            let lvbh = r["value"]["lastValidBlockHeight"].as_u64().unwrap_or(0);
            Ok((Hash::from_str(h).map_err(|e| anyhow!("{e:?}"))?, lvbh))
        }

        pub async fn get_slot(&self, commitment: &str) -> Result<u64> {
            self.call("getSlot", json!([{"commitment":commitment}]))
                .await?
                .as_u64()
                .ok_or_else(|| anyhow!("getSlot: bad result"))
        }

        pub async fn send_transaction(&self, wire: &[u8]) -> Result<String> {
            let b64 = base64::engine::general_purpose::STANDARD.encode(wire);
            let r = self
                .call(
                    "sendTransaction",
                    json!([b64, {"encoding":"base64","skipPreflight":false,"preflightCommitment":"confirmed","maxRetries":5}]),
                )
                .await?;
            Ok(r.as_str().unwrap_or_default().to_string())
        }

        pub async fn simulate(&self, wire: &[u8]) -> Result<Value> {
            let b64 = base64::engine::general_purpose::STANDARD.encode(wire);
            let r = self
                .call(
                    "simulateTransaction",
                    json!([b64, {"encoding":"base64","sigVerify":false,"replaceRecentBlockhash":true,"commitment":"processed"}]),
                )
                .await?;
            Ok(r["value"].clone())
        }

        pub async fn confirm(&self, sig: &str, timeout: Duration) -> Result<()> {
            let deadline = tokio::time::Instant::now() + timeout;
            loop {
                let r = self.call("getSignatureStatuses", json!([[sig]])).await?;
                let st = &r["value"][0];
                if !st.is_null() {
                    if !st["err"].is_null() {
                        bail!("tx {sig} failed: {}", st["err"]);
                    }
                    if matches!(st["confirmationStatus"].as_str(), Some("confirmed" | "finalized")) {
                        return Ok(());
                    }
                }
                if tokio::time::Instant::now() > deadline {
                    bail!("tx {sig} not confirmed within {timeout:?}");
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
        }
    }

    fn parse_account(v: &Value) -> Result<Option<RpcAccount>> {
        if v.is_null() {
            return Ok(None);
        }
        let b64 = v["data"][0].as_str().ok_or_else(|| anyhow!("account data missing"))?;
        Ok(Some(RpcAccount {
            lamports: v["lamports"].as_u64().unwrap_or(0),
            owner: Pubkey::from_str(v["owner"].as_str().unwrap_or_default())
                .map_err(|e| anyhow!("owner: {e}"))?,
            data: base64::engine::general_purpose::STANDARD.decode(b64)?,
        }))
    }
}

// ---------------------------------------------------------------------------------------------
// Kill switch: wallet balances are pushed by the gRPC stream (wallet + wSOL ATA are subscribed),
// reconciled by RPC on an interval. Checked before every tx is generated.
// ---------------------------------------------------------------------------------------------
pub(crate) mod guard {
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use tokio::sync::Notify;

    pub struct Guard {
        pub floor: u64,
        sol: AtomicU64,
        wsol: AtomicU64,
        sol_known: AtomicBool,
        wsol_known: AtomicBool,
        tripped: AtomicBool,
        pub notify: Notify,
    }

    impl Guard {
        pub fn new(floor: u64) -> Self {
            Self {
                floor,
                sol: AtomicU64::new(0),
                wsol: AtomicU64::new(0),
                sol_known: AtomicBool::new(false),
                wsol_known: AtomicBool::new(false),
                tripped: AtomicBool::new(false),
                notify: Notify::new(),
            }
        }
        pub fn set_sol(&self, v: u64) {
            self.sol.store(v, Ordering::Release);
            self.sol_known.store(true, Ordering::Release);
            self.evaluate();
        }
        pub fn set_wsol(&self, v: u64) {
            self.wsol.store(v, Ordering::Release);
            self.wsol_known.store(true, Ordering::Release);
            self.evaluate();
        }
        pub fn sol(&self) -> u64 {
            self.sol.load(Ordering::Acquire)
        }
        pub fn wsol(&self) -> u64 {
            self.wsol.load(Ordering::Acquire)
        }
        #[inline]
        pub fn is_tripped(&self) -> bool {
            self.tripped.load(Ordering::Acquire)
        }
        fn evaluate(&self) {
            if self.sol_known.load(Ordering::Acquire) && self.wsol_known.load(Ordering::Acquire) {
                let total = self.sol() + self.wsol();
                if total < self.floor {
                    self.trip(total);
                }
            }
        }
        fn trip(&self, total: u64) {
            if !self.tripped.swap(true, Ordering::AcqRel) {
                tracing::error!(
                    total_lamports = total,
                    floor_lamports = self.floor,
                    "KILL SWITCH: wallet balance below hard floor -- all executions aborted"
                );
                self.notify.notify_waiters();
                self.notify.notify_one();
            }
        }
        /// Gate for the executor. False => do not build/sign/send anything.
        #[inline]
        pub fn allow(&self) -> bool {
            if self.is_tripped() {
                return false;
            }
            if !self.sol_known.load(Ordering::Acquire) || !self.wsol_known.load(Ordering::Acquire) {
                return false;
            }
            let total = self.sol() + self.wsol();
            if total < self.floor {
                self.trip(total);
                return false;
            }
            true
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Market: account index + pool state + route matrix + evaluation
// ---------------------------------------------------------------------------------------------
pub(crate) mod market {
    use crate::config::{Config, PoolKind};
    use crate::consts::*;
    use crate::guard::Guard;
    use crate::ixs;
    use crate::layout::{self, u64_at};
    use crate::math::dlmm as dm;
    use crate::pools::{BinArraySlot, DlmmPool, Pool, RayPool, bin_array_pda};
    use crate::rpc::Rpc;
    use anyhow::{Result, bail};
    use solana_sdk::instruction::Instruction;
    use solana_sdk::pubkey::Pubkey;
    use std::collections::{HashMap, HashSet};
    use std::sync::Arc;
    use std::time::{Instant, SystemTime, UNIX_EPOCH};
    use tracing::{debug, info, warn};

    #[derive(Clone, Copy, Debug)]
    pub enum Role {
        RayAmm(u32),
        RayCoinVault(u32),
        RayPcVault(u32),
        DlmmPair(u32),
        DlmmBinArray(u32, i32),
        WalletSol,
        WalletWsol,
    }

    struct Tracked {
        role: Role,
        slot: u64,
        write_version: u64,
    }

    #[derive(Clone, Copy, Debug)]
    pub struct Leg {
        pub pool: u32,
        /// true = mint_a -> mint_b (ray coin->pc, dlmm x->y)
        pub a_to_b: bool,
        pub mint_in: Pubkey,
        pub mint_out: Pubkey,
    }

    pub struct Route {
        pub legs: [Leg; 3],
    }

    pub struct Opportunity {
        pub route: u32,
        pub swap_ixs: Vec<Instruction>,
        pub amount_in: u64,
        pub expected_out: u64,
        pub min_out: u64,
        pub tip: u64,
        pub profit: u64,
        pub slot: u64,
        pub created: Instant,
    }

    pub struct Params {
        pub min_trade: u64,
        pub max_trade: u64,
        pub min_profit: u64,
        pub tip_bps: u64,
        pub min_tip: u64,
        pub slippage_bps: u64,
        pub haircut_bps: u64,
        pub tx_fee: u64,
        pub max_arrays: usize,
        pub max_per_burst: usize,
    }

    pub struct Market {
        pub pools: Vec<Pool>,
        index: HashMap<Pubkey, Tracked>,
        pub routes: Vec<Route>,
        routes_by_pool: Vec<Vec<u32>>,
        pub wallet: Pubkey,
        pub wsol_ata: Pubkey,
        atas: HashMap<Pubkey, Pubkey>,
        pub latest_slot: u64,
        /// Account set changed => resubscribe.
        pub subs_dirty: bool,
        /// Newly tracked accounts whose current state must be fetched by RPC.
        pub pending_fetch: Vec<Pubkey>,
        /// False after reconnect until the resnapshot is applied (no trading on possibly-stale state).
        pub synced: bool,
        dirty: Vec<bool>,
        dirty_list: Vec<u32>,
        params: Params,
        guard: Arc<Guard>,
    }

    pub fn now_ts() -> i64 {
        SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
    }

    #[inline]
    fn haircut(x: u64, bps: u64) -> u64 {
        ((x as u128 * (10_000 - bps) as u128) / 10_000) as u64
    }

    impl Market {
        /// Bootstrap from RPC: parse pools, fetch vaults / bin arrays / wallet, build route matrix.
        pub async fn bootstrap(cfg: &Config, rpc: &Rpc, wallet: Pubkey, guard: Arc<Guard>) -> Result<Self> {
            let lamports = |sol: f64| (sol * LAMPORTS_PER_SOL) as u64;
            let r = &cfg.risk;
            let prio = (r.compute_unit_price_micro_lamports as u128 * r.compute_unit_limit as u128).div_ceil(1_000_000) as u64;
            let params = Params {
                min_trade: lamports(r.min_trade_sol),
                max_trade: lamports(r.max_trade_sol),
                min_profit: r.min_profit_lamports,
                tip_bps: r.tip_bps,
                min_tip: r.min_tip_lamports,
                slippage_bps: r.slippage_bps,
                haircut_bps: r.intermediate_haircut_bps,
                tx_fee: BASE_FEE_PER_SIG + prio,
                max_arrays: cfg.engine.dlmm_bin_arrays_per_side,
                max_per_burst: r.max_bundles_per_burst.max(1),
            };

            let mut addrs = Vec::new();
            let mut seen = HashSet::new();
            for p in &cfg.pools {
                let k: Pubkey = p.address.parse().map_err(|e| anyhow::anyhow!("pool {}: {e}", p.address))?;
                if seen.insert(k) {
                    addrs.push((k, p.kind));
                }
            }
            let keys: Vec<Pubkey> = addrs.iter().map(|a| a.0).collect();
            let (slot, accs) = rpc.get_multiple_accounts(&keys, "processed").await?;

            let mut pools = Vec::new();
            for ((key, kind), acc) in addrs.iter().zip(accs) {
                let Some(acc) = acc else {
                    warn!(%key, "pool account not found, skipped");
                    continue;
                };
                let parsed = match kind {
                    PoolKind::RaydiumAmmV4 if acc.owner == RAYDIUM_AMM_V4 => RayPool::parse(*key, &acc.data).map(Pool::Ray),
                    PoolKind::MeteoraDlmm if acc.owner == METEORA_DLMM => DlmmPool::parse(*key, &acc.data).map(Pool::Dlmm),
                    _ => None,
                };
                match parsed {
                    Some(p) => pools.push(p),
                    None => warn!(%key, ?kind, owner = %acc.owner, "unsupported/invalid pool (wrong owner, layout or Token-2022), skipped"),
                }
            }

            let wsol_ata = ixs::ata(&wallet, &WSOL_MINT);
            let mut m = Market {
                pools,
                index: HashMap::new(),
                routes: Vec::new(),
                routes_by_pool: Vec::new(),
                wallet,
                wsol_ata,
                atas: HashMap::new(),
                latest_slot: slot,
                subs_dirty: false,
                pending_fetch: Vec::new(),
                synced: true,
                dirty: Vec::new(),
                dirty_list: Vec::new(),
                params,
                guard,
            };

            m.build_routes(WSOL_MINT, cfg.engine.max_routes);
            if m.routes.is_empty() {
                bail!("no 3-leg wSOL cycles can be formed from the configured pools");
            }

            // ATAs for every mint on a route; routes through a missing ATA are dropped.
            let mut mints: Vec<Pubkey> = m.routes.iter().flat_map(|r| r.legs.iter().map(|l| l.mint_in)).collect();
            mints.sort();
            mints.dedup();
            let ata_keys: Vec<Pubkey> = mints.iter().map(|mint| ixs::ata(&wallet, mint)).collect();
            let (_, ata_accs) = rpc.get_multiple_accounts(&ata_keys, "confirmed").await?;
            let mut missing = HashSet::new();
            for ((mint, ata), acc) in mints.iter().zip(&ata_keys).zip(ata_accs) {
                if acc.is_some() {
                    m.atas.insert(*mint, *ata);
                } else {
                    warn!(%mint, %ata, "ATA missing -- run `searcher setup`; routes through this mint disabled");
                    missing.insert(*mint);
                }
            }
            if !m.atas.contains_key(&WSOL_MINT) {
                bail!("wSOL ATA {wsol_ata} missing -- run `searcher setup --wrap-sol <SOL>` first");
            }
            if !missing.is_empty() {
                m.routes.retain(|r| r.legs.iter().all(|l| !missing.contains(&l.mint_in)));
                m.index_routes();
            }

            // Track accounts.
            m.track(wallet, Role::WalletSol);
            m.track(wsol_ata, Role::WalletWsol);
            for i in 0..m.pools.len() as u32 {
                let addr = m.pools[i as usize].addr();
                match &m.pools[i as usize] {
                    Pool::Ray(p) => {
                        let (cv, pv) = (p.coin_vault, p.pc_vault);
                        m.track(addr, Role::RayAmm(i));
                        m.track(cv, Role::RayCoinVault(i));
                        m.track(pv, Role::RayPcVault(i));
                    }
                    Pool::Dlmm(_) => {
                        m.track(addr, Role::DlmmPair(i));
                        m.refresh_dlmm_arrays(i);
                    }
                }
            }
            m.dirty = vec![false; m.pools.len()];

            // Initial state for everything tracked.
            m.pending_fetch.clear();
            let all = m.tracked_keys();
            let (slot, accs) = rpc.get_multiple_accounts(&all, "processed").await?;
            for (k, a) in all.iter().zip(accs) {
                m.apply_rpc(k, a, slot);
            }
            // Bin arrays discovered from the freshly applied pair state.
            m.flush_pending_sync(rpc).await?;
            m.subs_dirty = false;

            let ready = m.pools.iter().filter(|p| p.ready()).count();
            info!(
                pools = m.pools.len(),
                ready,
                routes = m.routes.len(),
                tracked_accounts = m.index.len(),
                sol = m.guard.sol(),
                wsol = m.guard.wsol(),
                "market bootstrapped"
            );
            Ok(m)
        }

        async fn flush_pending_sync(&mut self, rpc: &Rpc) -> Result<()> {
            for _ in 0..4 {
                if self.pending_fetch.is_empty() {
                    break;
                }
                let keys = std::mem::take(&mut self.pending_fetch);
                let (slot, accs) = rpc.get_multiple_accounts(&keys, "processed").await?;
                for (k, a) in keys.iter().zip(accs) {
                    self.apply_rpc(k, a, slot);
                }
            }
            Ok(())
        }

        fn track(&mut self, k: Pubkey, role: Role) {
            self.index.insert(k, Tracked { role, slot: 0, write_version: 0 });
        }

        pub fn guard_tripped(&self) -> bool {
            self.guard.is_tripped()
        }

        pub fn tracked_keys(&self) -> Vec<Pubkey> {
            self.index.keys().copied().collect()
        }

        pub fn pool_addresses(&self) -> Vec<Pubkey> {
            self.pools.iter().map(Pool::addr).collect()
        }

        fn build_routes(&mut self, base: Pubkey, cap: usize) {
            let n = self.pools.len();
            let mints: Vec<(Pubkey, Pubkey)> = self.pools.iter().map(Pool::mints).collect();
            let leg = |p: usize, from: Pubkey| -> Option<Leg> {
                let (a, b) = mints[p];
                if from == a {
                    Some(Leg { pool: p as u32, a_to_b: true, mint_in: a, mint_out: b })
                } else if from == b {
                    Some(Leg { pool: p as u32, a_to_b: false, mint_in: b, mint_out: a })
                } else {
                    None
                }
            };
            'outer: for p1 in 0..n {
                let Some(l1) = leg(p1, base) else { continue };
                for p2 in 0..n {
                    if p2 == p1 {
                        continue;
                    }
                    let Some(l2) = leg(p2, l1.mint_out) else { continue };
                    if l2.mint_out == base || l2.mint_out == l1.mint_out {
                        continue;
                    }
                    for p3 in 0..n {
                        if p3 == p1 || p3 == p2 {
                            continue;
                        }
                        let Some(l3) = leg(p3, l2.mint_out) else { continue };
                        if l3.mint_out != base {
                            continue;
                        }
                        self.routes.push(Route { legs: [l1, l2, l3] });
                        if self.routes.len() >= cap {
                            warn!(cap, "route cap reached");
                            break 'outer;
                        }
                    }
                }
            }
            self.index_routes();
        }

        fn index_routes(&mut self) {
            self.routes_by_pool = vec![Vec::new(); self.pools.len()];
            for (i, r) in self.routes.iter().enumerate() {
                for l in &r.legs {
                    let v = &mut self.routes_by_pool[l.pool as usize];
                    if v.last() != Some(&(i as u32)) {
                        v.push(i as u32);
                    }
                }
            }
        }

        /// Keep subscriptions on exactly the liquid bin arrays a swap could traverse.
        fn refresh_dlmm_arrays(&mut self, pi: u32) {
            let take = self.params.max_arrays;
            let Pool::Dlmm(p) = &mut self.pools[pi as usize] else { return };
            let mut want = Vec::with_capacity(2 * take);
            p.liquid_arrays(true, take, &mut want);
            p.liquid_arrays(false, take, &mut want);
            want.sort_unstable();
            want.dedup();

            let mut added = Vec::new();
            for idx in &want {
                if !p.arrays.contains_key(idx) {
                    let key = bin_array_pda(&p.addr, *idx);
                    p.arrays.insert(*idx, BinArraySlot { key, data: None });
                    added.push((*idx, key));
                }
            }
            let stale: Vec<i32> = p.arrays.keys().copied().filter(|i| !want.contains(i)).collect();
            let mut removed = Vec::new();
            for i in stale {
                if let Some(s) = p.arrays.remove(&i) {
                    removed.push(s.key);
                }
            }
            for k in removed {
                self.index.remove(&k);
                self.subs_dirty = true;
            }
            for (idx, key) in added {
                self.index.insert(key, Tracked { role: Role::DlmmBinArray(pi, idx), slot: 0, write_version: 0 });
                self.pending_fetch.push(key);
                self.subs_dirty = true;
            }
        }

        /// Apply an RPC snapshot account (only if newer than what the stream already gave us).
        pub fn apply_rpc(&mut self, key: &Pubkey, acc: Option<crate::rpc::RpcAccount>, slot: u64) {
            let Some(t) = self.index.get(key) else { return };
            // Stream data (write_version > 0) wins ties: an RPC read at the same slot may predate it.
            if t.slot > slot || (t.slot == slot && t.write_version > 0) {
                return;
            }
            match acc {
                Some(a) => {
                    self.apply(key, a.data, a.lamports, slot, 0, 0, true);
                }
                None => {
                    if let Role::WalletWsol = t.role {
                        self.guard.set_wsol(0);
                    }
                }
            }
        }

        /// Apply an account write. `data` is moved in (zero-copy hand-off from the decoded frame).
        /// Returns true if pool state changed.
        #[allow(clippy::too_many_arguments)]
        pub fn apply(
            &mut self,
            key: &Pubkey,
            data: Vec<u8>,
            lamports: u64,
            slot: u64,
            write_version: u64,
            sig_tag: u64,
            from_rpc: bool,
        ) -> bool {
            let Some(t) = self.index.get_mut(key) else { return false };
            if !from_rpc && (slot < t.slot || (slot == t.slot && write_version < t.write_version)) {
                return false;
            }
            t.slot = slot;
            t.write_version = write_version;
            let role = t.role;
            if slot > self.latest_slot {
                self.latest_slot = slot;
            }
            match role {
                Role::WalletSol => {
                    self.guard.set_sol(lamports);
                    false
                }
                Role::WalletWsol => {
                    if data.len() >= layout::spl::LEN {
                        self.guard.set_wsol(u64_at(&data, layout::spl::AMOUNT));
                    }
                    false
                }
                Role::RayAmm(i) => {
                    if data.len() != layout::ray::LEN {
                        return false;
                    }
                    if let Pool::Ray(p) = &mut self.pools[i as usize] {
                        p.update_amm(&data);
                    }
                    self.mark(i);
                    true
                }
                Role::RayCoinVault(i) | Role::RayPcVault(i) => {
                    if data.len() < layout::spl::LEN {
                        return false;
                    }
                    let amt = u64_at(&data, layout::spl::AMOUNT);
                    if let Pool::Ray(p) = &mut self.pools[i as usize] {
                        if matches!(role, Role::RayCoinVault(_)) {
                            p.coin_amt = amt;
                            p.coin_sig = sig_tag;
                            p.has_coin = true;
                        } else {
                            p.pc_amt = amt;
                            p.pc_sig = sig_tag;
                            p.has_pc = true;
                        }
                    }
                    self.mark(i);
                    true
                }
                Role::DlmmPair(i) => {
                    if data.len() != layout::dlmm::LB_PAIR_LEN || data[..8] != layout::dlmm::LB_PAIR_DISC {
                        return false;
                    }
                    let mut refresh = false;
                    if let Pool::Dlmm(p) = &mut self.pools[i as usize] {
                        let (old_idx, old_bm) = (dm::bin_array_index(p.active_id), p.bitmap);
                        p.update_pair(&data);
                        refresh = old_idx != dm::bin_array_index(p.active_id) || old_bm != p.bitmap;
                    }
                    if refresh || from_rpc {
                        self.refresh_dlmm_arrays(i);
                    }
                    self.mark(i);
                    true
                }
                Role::DlmmBinArray(i, idx) => {
                    if data.len() != layout::dlmm::BIN_ARRAY_LEN || data[..8] != layout::dlmm::BIN_ARRAY_DISC {
                        return false;
                    }
                    if let Pool::Dlmm(p) = &mut self.pools[i as usize] {
                        // Reject anything that isn't exactly the array we derived for this pair.
                        if layout::i64_at(&data, layout::dlmm::BIN_ARRAY_INDEX) != idx as i64
                            || layout::pk_at(&data, layout::dlmm::BIN_ARRAY_LB_PAIR) != p.addr
                        {
                            return false;
                        }
                        if let Some(s) = p.arrays.get_mut(&idx) {
                            s.data = Some(data);
                        }
                    }
                    self.mark(i);
                    true
                }
            }
        }

        #[inline]
        fn mark(&mut self, i: u32) {
            if let Some(d) = self.dirty.get_mut(i as usize) {
                if !*d {
                    *d = true;
                    self.dirty_list.push(i);
                }
            }
        }

        #[inline]
        fn leg_quote(&self, l: &Leg, amount: u64, now: i64, used: Option<&mut Vec<i32>>) -> Option<u64> {
            match &self.pools[l.pool as usize] {
                Pool::Ray(p) => p.quote(amount, l.a_to_b, now),
                Pool::Dlmm(p) => p.quote(amount, l.a_to_b, now, self.latest_slot, self.params.max_arrays, used),
            }
        }

        /// Chain the 3 legs. Returns ([in1,in2,in3], [out1,out2,out3]).
        #[inline]
        fn simulate(&self, r: &Route, amount: u64, now: i64) -> Option<([u64; 3], [u64; 3])> {
            let h = self.params.haircut_bps;
            let o1 = self.leg_quote(&r.legs[0], amount, now, None)?;
            let a2 = haircut(o1, h);
            let o2 = self.leg_quote(&r.legs[1], a2, now, None)?;
            let a3 = haircut(o2, h);
            let o3 = self.leg_quote(&r.legs[2], a3, now, None)?;
            Some(([amount, a2, a3], [o1, o2, o3]))
        }

        #[inline]
        fn profit_at(&self, r: &Route, x: u64, now: i64) -> i128 {
            match self.simulate(r, x, now) {
                Some((_, o)) => o[2] as i128 - x as i128,
                None => i128::MIN / 4,
            }
        }

        /// Profit is concave in input size for x*y=k and bin-liquidity curves, so:
        /// (1) profit(min) <= 0 => no size is profitable; (2) golden-section search otherwise.
        fn optimize(&self, r: &Route, now: i64) -> Option<u64> {
            let mut lo = self.params.min_trade;
            let wsol = self.guard.wsol();
            let mut hi = self.params.max_trade.min(wsol);
            if hi < lo {
                return None;
            }
            if self.profit_at(r, lo, now) <= 0 {
                return None;
            }
            const INV_PHI: f64 = 0.618_033_988_749_895;
            let mut x1 = hi - ((hi - lo) as f64 * INV_PHI) as u64;
            let mut x2 = lo + ((hi - lo) as f64 * INV_PHI) as u64;
            let mut f1 = self.profit_at(r, x1, now);
            let mut f2 = self.profit_at(r, x2, now);
            for _ in 0..48 {
                if hi - lo <= (lo / 1_000).max(10_000) {
                    break;
                }
                if f1 < f2 {
                    lo = x1;
                    x1 = x2;
                    f1 = f2;
                    x2 = lo + ((hi - lo) as f64 * INV_PHI) as u64;
                    f2 = self.profit_at(r, x2, now);
                } else {
                    hi = x2;
                    x2 = x1;
                    f2 = f1;
                    x1 = hi - ((hi - lo) as f64 * INV_PHI) as u64;
                    f1 = self.profit_at(r, x1, now);
                }
            }
            let best = if f1 >= f2 { x1 } else { x2 };
            let fmin = self.profit_at(r, self.params.min_trade, now);
            if fmin > f1.max(f2) { Some(self.params.min_trade) } else { Some(best) }
        }

        /// Evaluate every route touching a pool updated in the last burst.
        pub fn evaluate_dirty(&mut self, out: &mut Vec<Opportunity>) {
            let list = std::mem::take(&mut self.dirty_list);
            for &i in &list {
                self.dirty[i as usize] = false;
            }
            if !self.synced || self.guard.is_tripped() {
                self.dirty_list = list;
                self.dirty_list.clear();
                return;
            }
            let now = now_ts();
            let mut seen: HashSet<u32> = HashSet::new();
            for &pi in &list {
                if !self.pools[pi as usize].ready() {
                    continue;
                }
                for &ri in &self.routes_by_pool[pi as usize] {
                    if !seen.insert(ri) {
                        continue;
                    }
                    let r = &self.routes[ri as usize];
                    if !r.legs.iter().all(|l| self.pools[l.pool as usize].ready()) {
                        continue;
                    }
                    if let Some(op) = self.price_route(ri, now) {
                        out.push(op);
                    }
                }
            }
            self.dirty_list = list;
            self.dirty_list.clear();
            out.sort_unstable_by(|a, b| b.profit.cmp(&a.profit));
            out.truncate(self.params.max_per_burst);
        }

        /// Pricing engine: expected_out > amount_in + fees + tip + min_profit, then build swaps.
        fn price_route(&self, ri: u32, now: i64) -> Option<Opportunity> {
            let r = &self.routes[ri as usize];
            let amount = self.optimize(r, now)?;
            let (ins, outs) = self.simulate(r, amount, now)?;
            let gross = outs[2].checked_sub(amount)?;
            let p = &self.params;
            let net = gross.checked_sub(p.tx_fee)?;
            let tip = ((net as u128 * p.tip_bps as u128) / 10_000) as u64;
            let tip = tip.max(p.min_tip);
            let profit = net.checked_sub(tip)?;
            if profit < p.min_profit {
                return None;
            }
            // On-chain revert conditions:
            //  - legs 1/2: min_out == the exact input of the next leg (any shortfall reverts)
            //  - leg 3: max(sim * (1 - slippage), in + tip + fee + min_profit) => never lands at a loss
            let slip_floor = ((outs[2] as u128 * (10_000 - p.slippage_bps) as u128) / 10_000) as u64;
            let profit_floor = amount + tip + p.tx_fee + p.min_profit;
            let min_out3 = slip_floor.max(profit_floor);
            let mins = [ins[1], ins[2], min_out3];

            let mut swap_ixs = Vec::with_capacity(3);
            let mut scratch = Vec::with_capacity(8);
            for (k, l) in r.legs.iter().enumerate() {
                let src = *self.atas.get(&l.mint_in)?;
                let dst = *self.atas.get(&l.mint_out)?;
                let ix = match &self.pools[l.pool as usize] {
                    Pool::Ray(pool) => ixs::raydium_swap(pool, src, dst, self.wallet, ins[k], mins[k]),
                    Pool::Dlmm(pool) => {
                        scratch.clear();
                        pool.quote(ins[k], l.a_to_b, now, self.latest_slot, p.max_arrays, Some(&mut scratch))?;
                        let keys: Vec<Pubkey> =
                            scratch.iter().map(|i| pool.arrays.get(i).map(|s| s.key)).collect::<Option<_>>()?;
                        ixs::dlmm_swap(pool, src, dst, self.wallet, &keys, ins[k], mins[k])
                    }
                };
                swap_ixs.push(ix);
            }
            debug!(route = ri, amount, out = outs[2], tip, profit, "opportunity");
            Some(Opportunity {
                route: ri,
                swap_ixs,
                amount_in: amount,
                expected_out: outs[2],
                min_out: min_out3,
                tip,
                profit,
                slot: self.latest_slot,
                created: Instant::now(),
            })
        }

        pub fn describe_route(&self, ri: u32) -> String {
            let r = &self.routes[ri as usize];
            let short = |k: &Pubkey| {
                let s = k.to_string();
                s[..4.min(s.len())].to_string()
            };
            format!(
                "{} -> {} -> {} -> {}",
                short(&r.legs[0].mint_in),
                short(&r.legs[1].mint_in),
                short(&r.legs[2].mint_in),
                short(&r.legs[2].mint_out)
            )
        }

        /// Static (non-signer, non-program-invoked) keys worth putting in an address lookup table.
        pub fn alt_candidates(&self) -> Vec<Pubkey> {
            let mut v = vec![SPL_TOKEN, ixs::raydium_authority(), ixs::dlmm_event_authority()];
            v.extend(self.atas.values().copied());
            v.extend(JITO_TIP_ACCOUNTS);
            for p in &self.pools {
                match p {
                    Pool::Ray(p) => v.extend([p.addr, p.coin_vault, p.pc_vault]),
                    Pool::Dlmm(p) => v.extend([p.addr, p.reserve_x, p.reserve_y, p.mint_x, p.mint_y, p.oracle]),
                }
            }
            v.sort();
            v.dedup();
            v
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Jito block-engine client: sendBundle fan-out with per-endpoint token-bucket rate limiting
// ---------------------------------------------------------------------------------------------
pub(crate) mod jito {
    use anyhow::Result;
    use base64::Engine;
    use serde_json::json;
    use solana_sdk::pubkey::Pubkey;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, Instant};
    use tracing::{debug, info, warn};

    struct Endpoint {
        url: String,
        next_ok_ns: AtomicU64,
    }

    pub struct Jito {
        http: reqwest::Client,
        endpoints: Vec<Endpoint>,
        interval_ns: u64,
        uuid: Option<String>,
        epoch: Instant,
        pub sent: AtomicU64,
        pub errors: AtomicU64,
    }

    impl Jito {
        pub fn new(urls: &[String], rps: f64, uuid: Option<String>) -> Result<Self> {
            let http = reqwest::Client::builder()
                .tcp_nodelay(true)
                .timeout(Duration::from_millis(1_500))
                .pool_max_idle_per_host(4)
                .http2_keep_alive_interval(Duration::from_secs(10))
                .build()?;
            Ok(Self {
                http,
                endpoints: urls
                    .iter()
                    .map(|u| Endpoint { url: format!("{}/api/v1/bundles", u.trim_end_matches('/')), next_ok_ns: AtomicU64::new(0) })
                    .collect(),
                interval_ns: (1e9 / rps) as u64,
                uuid,
                epoch: Instant::now(),
                sent: AtomicU64::new(0),
                errors: AtomicU64::new(0),
            })
        }

        fn acquire(&self, e: &Endpoint) -> bool {
            let now = self.epoch.elapsed().as_nanos() as u64;
            let next = e.next_ok_ns.load(Ordering::Acquire);
            now >= next
                && e.next_ok_ns
                    .compare_exchange(next, now + self.interval_ns, Ordering::AcqRel, Ordering::Acquire)
                    .is_ok()
        }

        /// Fire-and-forget to every endpoint with rate budget left. Returns endpoints used.
        pub fn send_bundle(self: &Arc<Self>, wire: &[u8], tag: u32) -> usize {
            let b64 = base64::engine::general_purpose::STANDARD.encode(wire);
            let body = Arc::new(json!({
                "jsonrpc": "2.0", "id": 1, "method": "sendBundle",
                "params": [[b64], {"encoding": "base64"}]
            }));
            let mut n = 0;
            for i in 0..self.endpoints.len() {
                if !self.acquire(&self.endpoints[i]) {
                    continue;
                }
                n += 1;
                let me = Arc::clone(self);
                let body = Arc::clone(&body);
                tokio::spawn(async move {
                    let ep = &me.endpoints[i];
                    let mut req = me.http.post(&ep.url).json(&*body);
                    if let Some(u) = &me.uuid {
                        req = req.header("x-jito-auth", u);
                    }
                    match req.send().await {
                        Ok(resp) => {
                            let status = resp.status();
                            let text = resp.text().await.unwrap_or_default();
                            if status.is_success() && !text.contains("\"error\"") {
                                me.sent.fetch_add(1, Ordering::Relaxed);
                                debug!(route = tag, endpoint = %ep.url, resp = %text, "bundle accepted");
                            } else {
                                me.errors.fetch_add(1, Ordering::Relaxed);
                                debug!(route = tag, endpoint = %ep.url, %status, resp = %text, "bundle rejected");
                            }
                        }
                        Err(e) => {
                            me.errors.fetch_add(1, Ordering::Relaxed);
                            debug!(route = tag, endpoint = %ep.url, err = %e, "bundle send failed");
                        }
                    }
                });
            }
            n
        }

        pub async fn tip_accounts(&self) -> Option<Vec<Pubkey>> {
            let ep = self.endpoints.first()?;
            let body = json!({"jsonrpc":"2.0","id":1,"method":"getTipAccounts","params":[]});
            let resp = self.http.post(&ep.url).json(&body).send().await.ok()?;
            let v: serde_json::Value = resp.json().await.ok()?;
            let list: Vec<Pubkey> = v["result"].as_array()?.iter().filter_map(|s| s.as_str()?.parse().ok()).collect();
            if list.is_empty() {
                warn!("getTipAccounts returned nothing; using static list");
                return None;
            }
            info!(n = list.len(), "tip accounts loaded from block engine");
            Some(list)
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Executor: kill switch -> compile v0 (with ALTs) -> sign once -> wire bytes -> Jito / simulate
// ---------------------------------------------------------------------------------------------
pub(crate) mod executor {
    use crate::consts::*;
    use crate::guard::Guard;
    use crate::ixs;
    use crate::jito::Jito;
    use crate::market::Opportunity;
    use crate::rpc::Rpc;
    use solana_sdk::hash::Hash;
    use solana_sdk::message::{AddressLookupTableAccount, VersionedMessage, v0};
    use solana_sdk::pubkey::Pubkey;
    use solana_sdk::signature::Keypair;
    use solana_sdk::signer::Signer;
    use std::collections::HashMap;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, Instant};
    use tokio::sync::{mpsc, watch};
    use tracing::{debug, error, info, warn};

    pub struct ExecCfg {
        pub cu_limit: u32,
        pub cu_price: u64,
        pub cooldown: Duration,
        pub max_age: Duration,
        pub dry_run: bool,
    }

    pub struct Stats {
        pub opportunities: AtomicU64,
        pub built: AtomicU64,
        pub oversize: AtomicU64,
        pub sim_ok: AtomicU64,
        pub sim_fail: AtomicU64,
    }

    /// Serialize -> sign the message bytes once -> splice [1 | sig | msg]. No second encode pass.
    #[inline]
    pub fn sign_v0(kp: &Keypair, msg: v0::Message) -> Vec<u8> {
        let bytes = VersionedMessage::V0(msg).serialize();
        let sig = kp.sign_message(&bytes);
        let mut wire = Vec::with_capacity(1 + 64 + bytes.len());
        wire.push(1u8);
        wire.extend_from_slice(sig.as_ref());
        wire.extend_from_slice(&bytes);
        wire
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn run(
        mut rx: mpsc::Receiver<Opportunity>,
        kp: Arc<Keypair>,
        alts: Vec<AddressLookupTableAccount>,
        tip_accounts: Vec<Pubkey>,
        blockhash: watch::Receiver<(Hash, Instant)>,
        guard: Arc<Guard>,
        jito: Arc<Jito>,
        rpc: Arc<Rpc>,
        cfg: ExecCfg,
        stats: Arc<Stats>,
        route_names: Arc<Vec<String>>,
    ) {
        let payer = kp.pubkey();
        let mut last_fire: HashMap<u32, Instant> = HashMap::new();
        let mut last_sim = Instant::now() - Duration::from_secs(1);
        let mut warned_oversize: std::collections::HashSet<u32> = Default::default();

        while let Some(op) = rx.recv().await {
            stats.opportunities.fetch_add(1, Ordering::Relaxed);

            // ---- KILL SWITCH: evaluated before every transaction generation ----
            if !guard.allow() {
                if guard.is_tripped() {
                    error!("executor halted by kill switch");
                    return;
                }
                debug!("wallet balances not yet known; skipping");
                continue;
            }
            if op.created.elapsed() > cfg.max_age {
                debug!(route = op.route, "opportunity stale, dropped");
                continue;
            }
            if let Some(t) = last_fire.get(&op.route) {
                if t.elapsed() < cfg.cooldown {
                    continue;
                }
            }
            // Native SOL must cover tip + fee and stay rent-exempt; profit accrues in wSOL.
            let fee = BASE_FEE_PER_SIG + (cfg.cu_price as u128 * cfg.cu_limit as u128).div_ceil(1_000_000) as u64;
            if guard.sol() < op.tip + fee + RENT_EXEMPT_FLOOR {
                warn!(sol = guard.sol(), tip = op.tip, "native SOL too low for tip; top up the payer");
                continue;
            }
            let (hash, fetched) = *blockhash.borrow();
            if fetched.elapsed() > Duration::from_secs(20) || hash == Hash::default() {
                warn!("blockhash stale (RPC down?); skipping");
                continue;
            }

            let tip_to = tip_accounts[(rand::random::<u32>() as usize) % tip_accounts.len()];
            let mut ix = Vec::with_capacity(6);
            ix.push(ixs::cu_limit(cfg.cu_limit));
            if cfg.cu_price > 0 {
                ix.push(ixs::cu_price(cfg.cu_price));
            }
            ix.extend(op.swap_ixs);
            ix.push(ixs::transfer(payer, tip_to, op.tip));

            let msg = match v0::Message::try_compile(&payer, &ix, &alts, hash) {
                Ok(m) => m,
                Err(e) => {
                    error!(err = ?e, "message compile failed");
                    continue;
                }
            };
            let wire = sign_v0(&kp, msg);
            if wire.len() > MAX_TX_SIZE {
                stats.oversize.fetch_add(1, Ordering::Relaxed);
                if warned_oversize.insert(op.route) {
                    warn!(route = op.route, size = wire.len(), "tx exceeds 1232 bytes -- add pool accounts to an ALT (`searcher setup --create-alt`)");
                }
                continue;
            }
            stats.built.fetch_add(1, Ordering::Relaxed);
            last_fire.insert(op.route, Instant::now());
            let name = &route_names[op.route as usize];

            if cfg.dry_run {
                if last_sim.elapsed() < Duration::from_millis(200) {
                    continue;
                }
                last_sim = Instant::now();
                let rpc = Arc::clone(&rpc);
                let stats = Arc::clone(&stats);
                let name = name.clone();
                let (amount, profit, tip, slot) = (op.amount_in, op.profit, op.tip, op.slot);
                tokio::spawn(async move {
                    match rpc.simulate(&wire).await {
                        Ok(v) if v["err"].is_null() => {
                            stats.sim_ok.fetch_add(1, Ordering::Relaxed);
                            info!(route = %name, amount, profit, tip, slot, units = %v["unitsConsumed"], "[dry-run] simulation OK");
                        }
                        Ok(v) => {
                            stats.sim_fail.fetch_add(1, Ordering::Relaxed);
                            let logs = v["logs"].as_array().map(|l| {
                                l.iter().rev().take(4).filter_map(|x| x.as_str()).collect::<Vec<_>>().join(" | ")
                            });
                            info!(route = %name, amount, err = %v["err"], logs = ?logs, "[dry-run] simulation reverted");
                        }
                        Err(e) => warn!(err = %e, "[dry-run] simulate call failed"),
                    }
                });
                continue;
            }

            let n = jito.send_bundle(&wire, op.route);
            if n == 0 {
                debug!(route = op.route, "all block engines rate-limited; dropped");
            } else {
                info!(
                    route = %name, amount = op.amount_in, expected_out = op.expected_out,
                    min_out = op.min_out, tip = op.tip, profit = op.profit, slot = op.slot,
                    latency_us = op.created.elapsed().as_micros() as u64, endpoints = n,
                    "bundle sent"
                );
            }
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Ingestion daemon: Yellowstone gRPC with stall watchdog + immediate reconnect
// ---------------------------------------------------------------------------------------------
pub(crate) mod ingest {
    use crate::config::Config;
    use crate::market::{Market, Opportunity};
    use crate::rpc::{Rpc, RpcAccount};
    use anyhow::{Context, Result, anyhow};
    use futures::{FutureExt, SinkExt, StreamExt};
    use solana_sdk::pubkey::Pubkey;
    use std::collections::HashMap;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, Instant};
    use tokio::sync::mpsc;
    use tracing::{debug, error, info, warn};
    use yellowstone_grpc_client::{ClientTlsConfig, GeyserGrpcClient};
    use yellowstone_grpc_proto::prelude::{
        CommitmentLevel, SubscribeRequest, SubscribeRequestFilterAccounts, SubscribeRequestFilterSlots,
        SubscribeRequestFilterTransactions, SubscribeRequestPing, SubscribeUpdate, subscribe_update::UpdateOneof,
    };

    pub struct IngestStats {
        pub updates: AtomicU64,
        pub txs: AtomicU64,
        pub reconnects: AtomicU64,
        pub eval_ns_last: AtomicU64,
        pub eval_ns_max: AtomicU64,
    }

    struct Snapshot {
        slot: u64,
        keys: Vec<Pubkey>,
        accounts: Vec<Option<RpcAccount>>,
        full: bool,
    }

    fn build_request(m: &Market, with_txs: bool) -> SubscribeRequest {
        let mut accounts = HashMap::new();
        accounts.insert(
            "state".to_string(),
            SubscribeRequestFilterAccounts {
                account: m.tracked_keys().iter().map(|k| k.to_string()).collect(),
                ..Default::default()
            },
        );
        let mut slots = HashMap::new();
        slots.insert(
            "heartbeat".to_string(),
            SubscribeRequestFilterSlots { filter_by_commitment: Some(true), ..Default::default() },
        );
        let mut transactions = HashMap::new();
        if with_txs {
            transactions.insert(
                "pools".to_string(),
                SubscribeRequestFilterTransactions {
                    vote: Some(false),
                    failed: Some(false),
                    account_include: m.pool_addresses().iter().map(|k| k.to_string()).collect(),
                    ..Default::default()
                },
            );
        }
        SubscribeRequest {
            accounts,
            slots,
            transactions,
            commitment: Some(CommitmentLevel::Processed as i32),
            ..Default::default()
        }
    }

    fn spawn_snapshot(rpc: Arc<Rpc>, keys: Vec<Pubkey>, full: bool, tx: mpsc::Sender<Snapshot>) {
        tokio::spawn(async move {
            for attempt in 0..5u32 {
                match rpc.get_multiple_accounts(&keys, "processed").await {
                    Ok((slot, accounts)) => {
                        let _ = tx.send(Snapshot { slot, keys, accounts, full }).await;
                        return;
                    }
                    Err(e) => {
                        warn!(err = %e, attempt, "snapshot fetch failed");
                        tokio::time::sleep(Duration::from_millis(100 << attempt)).await;
                    }
                }
            }
            error!("snapshot fetch gave up");
        });
    }

    /// Daemon loop. Never returns unless the kill switch trips.
    pub async fn run(
        cfg: Arc<Config>,
        mut market: Market,
        rpc: Arc<Rpc>,
        exec_tx: mpsc::Sender<Opportunity>,
        stats: Arc<IngestStats>,
    ) {
        let base = Duration::from_millis(cfg.network.reconnect_base_delay_ms);
        let mut failures: u32 = 0;
        let (snap_tx, mut snap_rx) = mpsc::channel::<Snapshot>(16);
        loop {
            if market_guard_tripped(&market) {
                return;
            }
            let started = Instant::now();
            let res = session(&cfg, &mut market, &rpc, &exec_tx, &stats, &snap_tx, &mut snap_rx).await;
            if market_guard_tripped(&market) {
                return;
            }
            stats.reconnects.fetch_add(1, Ordering::Relaxed);
            market.synced = false;
            if started.elapsed() > Duration::from_secs(10) {
                failures = 0;
            }
            let delay = if failures == 0 { base } else { (base * 2u32.saturating_pow(failures)).min(Duration::from_secs(2)) };
            failures = failures.saturating_add(1);
            match res {
                Ok(()) => warn!(?delay, "gRPC stream ended; reconnecting"),
                Err(e) => warn!(err = %format!("{e:#}"), ?delay, "gRPC session error; reconnecting"),
            }
            tokio::time::sleep(delay).await;
        }
    }

    fn market_guard_tripped(m: &Market) -> bool {
        m.guard_tripped()
    }

    async fn connect(cfg: &Config) -> Result<GeyserGrpcClient> {
        let mut b = GeyserGrpcClient::build_from_shared(cfg.network.grpc_endpoint.clone())?
            .x_token(cfg.grpc_x_token())?
            .connect_timeout(Duration::from_secs(2))
            .tcp_nodelay(true)
            .http2_adaptive_window(true)
            .http2_keep_alive_interval(Duration::from_secs(1))
            .keep_alive_timeout(Duration::from_secs(2))
            .keep_alive_while_idle(true)
            .max_decoding_message_size(256 * 1024 * 1024);
        if cfg.network.grpc_endpoint.starts_with("https") {
            b = b.tls_config(ClientTlsConfig::new().with_native_roots())?;
        }
        Ok(b.connect().await?)
    }

    async fn session(
        cfg: &Config,
        market: &mut Market,
        rpc: &Arc<Rpc>,
        exec_tx: &mpsc::Sender<Opportunity>,
        stats: &IngestStats,
        snap_tx: &mpsc::Sender<Snapshot>,
        snap_rx: &mut mpsc::Receiver<Snapshot>,
    ) -> Result<()> {
        let with_txs = cfg.engine.subscribe_transactions;
        let mut client = tokio::time::timeout(Duration::from_secs(3), connect(cfg))
            .await
            .map_err(|_| anyhow!("connect timeout"))?
            .context("connect")?;
        let (mut sink, mut stream) = client
            .subscribe_with_request(Some(build_request(market, with_txs)))
            .await
            .context("subscribe")?;
        info!(endpoint = %cfg.network.grpc_endpoint, accounts = market.tracked_keys().len(), "gRPC subscribed");

        // Close the gap between the last state we have and the stream start.
        spawn_snapshot(Arc::clone(rpc), market.tracked_keys(), true, snap_tx.clone());

        let stall = Duration::from_millis(cfg.network.stream_stall_timeout_ms);
        let mut opps = Vec::with_capacity(16);
        loop {
            tokio::select! {
                biased;
                msg = tokio::time::timeout(stall, stream.next()) => {
                    let msg = match msg {
                        Err(_) => return Err(anyhow!("stream stalled > {stall:?}")),
                        Ok(None) => return Ok(()),
                        Ok(Some(Err(status))) => return Err(anyhow!("stream status: {status}")),
                        Ok(Some(Ok(u))) => u,
                    };
                    let mut need_ping = handle(market, msg, stats);
                    // Drain the burst already buffered so multi-account writes of one tx are
                    // applied together before evaluation.
                    for _ in 0..1024 {
                        match stream.next().now_or_never() {
                            Some(Some(Ok(u))) => need_ping |= handle(market, u, stats),
                            Some(Some(Err(status))) => return Err(anyhow!("stream status: {status}")),
                            Some(None) => return Ok(()),
                            None => break,
                        }
                    }
                    if need_ping {
                        sink.send(SubscribeRequest { ping: Some(SubscribeRequestPing { id: 1 }), ..Default::default() })
                            .await
                            .map_err(|e| anyhow!("ping send: {e}"))?;
                    }
                    let t0 = Instant::now();
                    market.evaluate_dirty(&mut opps);
                    let ns = t0.elapsed().as_nanos() as u64;
                    stats.eval_ns_last.store(ns, Ordering::Relaxed);
                    stats.eval_ns_max.fetch_max(ns, Ordering::Relaxed);
                    for op in opps.drain(..) {
                        if exec_tx.try_send(op).is_err() {
                            debug!("executor queue full; opportunity dropped");
                        }
                    }
                    if !market.pending_fetch.is_empty() {
                        spawn_snapshot(Arc::clone(rpc), std::mem::take(&mut market.pending_fetch), false, snap_tx.clone());
                    }
                    if market.subs_dirty {
                        market.subs_dirty = false;
                        sink.send(build_request(market, with_txs)).await.map_err(|e| anyhow!("resubscribe: {e}"))?;
                        debug!(accounts = market.tracked_keys().len(), "subscription updated");
                    }
                }
                Some(snap) = snap_rx.recv() => {
                    for (k, a) in snap.keys.iter().zip(snap.accounts) {
                        market.apply_rpc(k, a, snap.slot);
                    }
                    if snap.full && !market.synced {
                        market.synced = true;
                        info!(slot = snap.slot, "state resynced after reconnect");
                    }
                }
            }
        }
    }

    /// Returns true if the server pinged (client must answer to keep LBs alive).
    #[inline]
    fn handle(market: &mut Market, u: SubscribeUpdate, stats: &IngestStats) -> bool {
        match u.update_oneof {
            Some(UpdateOneof::Account(acc)) => {
                let slot = acc.slot;
                let Some(mut info) = acc.account else { return false };
                let Ok(key) = <[u8; 32]>::try_from(info.pubkey.as_slice()) else { return false };
                let key = Pubkey::new_from_array(key);
                let sig_tag = info
                    .txn_signature
                    .as_deref()
                    .and_then(|s| s.get(..8))
                    .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
                    .unwrap_or(0);
                stats.updates.fetch_add(1, Ordering::Relaxed);
                // Move the decoded buffer into state: zero additional copies.
                let data = std::mem::take(&mut info.data);
                market.apply(&key, data, info.lamports, slot, info.write_version, sig_tag, false);
                false
            }
            Some(UpdateOneof::Slot(s)) => {
                if s.slot > market.latest_slot {
                    market.latest_slot = s.slot;
                }
                false
            }
            Some(UpdateOneof::Transaction(_)) => {
                stats.txs.fetch_add(1, Ordering::Relaxed);
                false
            }
            Some(UpdateOneof::Ping(_)) => true,
            _ => false,
        }
    }
}

// ---------------------------------------------------------------------------------------------
// One-time wallet setup: ATAs, wSOL wrap, address lookup table
// ---------------------------------------------------------------------------------------------
pub(crate) mod setup {
    use crate::config::Config;
    use crate::consts::*;
    use crate::guard::Guard;
    use crate::ixs;
    use crate::market::Market;
    use crate::rpc::Rpc;
    use anyhow::{Context, Result};
    use solana_sdk::instruction::Instruction;
    use solana_sdk::message::{Message, VersionedMessage};
    use solana_sdk::pubkey::Pubkey;
    use solana_sdk::signature::Keypair;
    use solana_sdk::signer::Signer;
    use std::sync::Arc;
    use std::time::Duration;
    use tracing::info;

    async fn send(rpc: &Rpc, kp: &Keypair, ixs: &[Instruction]) -> Result<String> {
        let (hash, _) = rpc.latest_blockhash().await?;
        let msg = Message::new_with_blockhash(ixs, Some(&kp.pubkey()), &hash);
        let bytes = VersionedMessage::Legacy(msg).serialize();
        let sig = kp.sign_message(&bytes);
        let mut wire = vec![1u8];
        wire.extend_from_slice(sig.as_ref());
        wire.extend_from_slice(&bytes);
        let s = rpc.send_transaction(&wire).await?;
        rpc.confirm(&s, Duration::from_secs(90)).await?;
        Ok(s)
    }

    pub async fn run(cfg: &Config, kp: &Keypair, wrap_sol: Option<f64>, create_alt: bool) -> Result<()> {
        let rpc = Rpc::new(&cfg.network.rpc_endpoint)?;
        let me = kp.pubkey();

        // Mints = every mint on the configured pools (parse pools directly; ATAs may not exist yet).
        let keys: Vec<Pubkey> = cfg.pools.iter().map(|p| p.address.parse()).collect::<Result<_, _>>()?;
        let (_, accs) = rpc.get_multiple_accounts(&keys, "confirmed").await?;
        let mut mints = vec![WSOL_MINT];
        for a in accs.into_iter().flatten() {
            if a.owner == RAYDIUM_AMM_V4 {
                if let Some(p) = crate::pools::RayPool::parse(Pubkey::default(), &a.data) {
                    mints.extend([p.coin_mint, p.pc_mint]);
                }
            } else if a.owner == METEORA_DLMM {
                if let Some(p) = crate::pools::DlmmPool::parse(Pubkey::default(), &a.data) {
                    mints.extend([p.mint_x, p.mint_y]);
                }
            }
        }
        mints.sort();
        mints.dedup();

        let atas: Vec<Pubkey> = mints.iter().map(|m| ixs::ata(&me, m)).collect();
        let (_, existing) = rpc.get_multiple_accounts(&atas, "confirmed").await?;
        let to_create: Vec<Pubkey> =
            mints.iter().zip(existing).filter(|(_, e)| e.is_none()).map(|(m, _)| *m).collect();
        for chunk in to_create.chunks(6) {
            let ixs: Vec<Instruction> = chunk.iter().map(|m| ixs::create_ata_idempotent(me, me, *m)).collect();
            let sig = send(&rpc, kp, &ixs).await.context("create ATAs")?;
            info!(n = chunk.len(), %sig, "ATAs created");
        }

        if let Some(sol) = wrap_sol {
            let lamports = (sol * LAMPORTS_PER_SOL) as u64;
            let wsol = ixs::ata(&me, &WSOL_MINT);
            let sig = send(&rpc, kp, &[ixs::transfer(me, wsol, lamports), ixs::sync_native(wsol)])
                .await
                .context("wrap SOL")?;
            info!(sol, %sig, "SOL wrapped into wSOL ATA");
        }

        if create_alt {
            let guard = Arc::new(Guard::new(1));
            let market = Market::bootstrap(cfg, &rpc, me, guard).await?;
            let keys = market.alt_candidates();
            let slot = rpc.get_slot("finalized").await?;
            let (ix, table) = ixs::alt_create(me, me, slot);
            let sig = send(&rpc, kp, &[ix]).await.context("create ALT")?;
            info!(%table, %sig, "lookup table created");
            for chunk in keys.chunks(25) {
                let sig = send(&rpc, kp, &[ixs::alt_extend(table, me, me, chunk)]).await.context("extend ALT")?;
                info!(n = chunk.len(), %sig, "lookup table extended");
            }
            println!("\nAdd to config [engine]:\n  lookup_tables = [\"{table}\"]\n");
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------------------------
// Entrypoint
// ---------------------------------------------------------------------------------------------
use anyhow::{Context, Result, anyhow, bail};
use clap::{Parser, Subcommand};
use solana_sdk::hash::Hash;
use solana_sdk::message::AddressLookupTableAccount;
use solana_sdk::pubkey::Pubkey;
use solana_sdk::signer::Signer;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tracing::{error, info, warn};

#[derive(Parser)]
#[command(name = "searcher", version, about = "Raydium/Meteora triangular arbitrage searcher (Jito)")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the searcher daemon.
    Run {
        #[arg(short, long, default_value = "/etc/mev-searcher/config.toml")]
        config: PathBuf,
    },
    /// Encrypt a Solana CLI keypair JSON (64-byte array) into the keystore format.
    EncryptKey {
        #[arg(long)]
        input: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    /// One-time setup: create ATAs, optionally wrap SOL and create an address lookup table.
    Setup {
        #[arg(short, long, default_value = "/etc/mev-searcher/config.toml")]
        config: PathBuf,
        #[arg(long)]
        wrap_sol: Option<f64>,
        #[arg(long)]
        create_alt: bool,
    },
}

fn init_tracing() {
    use tracing_subscriber::EnvFilter;
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .with_target(false)
        .with_ansi(std::io::IsTerminal::is_terminal(&std::io::stdout()))
        .compact()
        .init();
}

fn load_keypair(cfg: &config::Config) -> Result<solana_sdk::signature::Keypair> {
    let pass = keystore::resolve_passphrase(&cfg.wallet.passphrase_env, cfg.wallet.passphrase_file.as_deref())?;
    let kp = keystore::decrypt_file(&cfg.wallet.encrypted_keypair_path, pass.as_bytes())?;
    // Scrub the passphrase from the process environment before any other thread exists.
    // SAFETY: called on the main thread before the tokio runtime (or any other thread) is started.
    unsafe { std::env::remove_var(&cfg.wallet.passphrase_env) };
    Ok(kp)
}

fn main() {
    init_tracing();
    let cli = Cli::parse();
    let code = match cli.cmd {
        Cmd::EncryptKey { input, output } => report(encrypt_key(&input, &output)),
        Cmd::Setup { config, wrap_sol, create_alt } => report((|| {
            let cfg = config::Config::load(&config)?;
            let kp = load_keypair(&cfg)?;
            runtime(2)?.block_on(setup::run(&cfg, &kp, wrap_sol, create_alt))
        })()),
        Cmd::Run { config } => match (|| {
            let cfg = config::Config::load(&config)?;
            let kp = load_keypair(&cfg)?;
            let rt = runtime(cfg.engine.worker_threads)?;
            rt.block_on(run(cfg, kp))
        })() {
            Ok(code) => code,
            Err(e) => {
                error!("fatal: {e:#}");
                1
            }
        },
    };
    std::process::exit(code);
}

fn report(r: Result<()>) -> i32 {
    match r {
        Ok(()) => 0,
        Err(e) => {
            error!("{e:#}");
            1
        }
    }
}

fn runtime(threads: usize) -> Result<tokio::runtime::Runtime> {
    Ok(tokio::runtime::Builder::new_multi_thread()
        .worker_threads(threads.max(2))
        .thread_name("searcher")
        .enable_all()
        .build()?)
}

fn encrypt_key(input: &std::path::Path, output: &std::path::Path) -> Result<()> {
    let raw = zeroize::Zeroizing::new(std::fs::read_to_string(input)?);
    let v: Vec<u8> = serde_json::from_str(&raw).context("keypair JSON must be a [u8; 64] array")?;
    let mut arr = zeroize::Zeroizing::new([0u8; 64]);
    if v.len() != 64 {
        bail!("expected 64 bytes, got {}", v.len());
    }
    arr.copy_from_slice(&v);
    drop(zeroize::Zeroizing::new(v));
    let kp = solana_sdk::signature::Keypair::new_from_array(arr[..32].try_into().unwrap());
    if kp.pubkey().to_bytes()[..] != arr[32..] {
        bail!("keypair file is inconsistent (pubkey mismatch)");
    }
    let p1 = zeroize::Zeroizing::new(match std::env::var("MEV_KEY_PASSPHRASE") {
        Ok(p) if !p.is_empty() => p,
        _ => {
            let a = rpassword::prompt_password("new passphrase: ")?;
            let b = rpassword::prompt_password("repeat passphrase: ")?;
            if a != b {
                bail!("passphrases differ");
            }
            a
        }
    });
    if p1.len() < 12 {
        bail!("passphrase must be at least 12 characters");
    }
    keystore::encrypt_to_file(&arr, p1.as_bytes(), output)?;
    info!(pubkey = %kp.pubkey(), path = %output.display(), "keystore written (mode 600)");
    Ok(())
}

async fn load_alts(rpc: &rpc::Rpc, keys: &[String]) -> Result<Vec<AddressLookupTableAccount>> {
    if keys.is_empty() {
        return Ok(vec![]);
    }
    let pks: Vec<Pubkey> = keys.iter().map(|k| k.parse()).collect::<Result<_, _>>().map_err(|e| anyhow!("ALT key: {e}"))?;
    let (_, accs) = rpc.get_multiple_accounts(&pks, "confirmed").await?;
    let mut out = Vec::new();
    for (k, a) in pks.iter().zip(accs) {
        let a = a.ok_or_else(|| anyhow!("lookup table {k} not found"))?;
        if a.owner != consts::ALT_PROGRAM || a.data.len() < 56 {
            bail!("{k} is not an address lookup table");
        }
        let addresses = a.data[56..].chunks_exact(32).map(|c| Pubkey::new_from_array(c.try_into().unwrap())).collect::<Vec<_>>();
        info!(table = %k, n = addresses.len(), "lookup table loaded");
        out.push(AddressLookupTableAccount { key: *k, addresses });
    }
    Ok(out)
}

async fn run(cfg: config::Config, kp: solana_sdk::signature::Keypair) -> Result<i32> {
    let cfg = Arc::new(cfg);
    let kp = Arc::new(kp);
    let wallet = kp.pubkey();
    info!(%wallet, dry_run = cfg.risk.dry_run, "searcher starting");

    let rpc = Arc::new(rpc::Rpc::new(&cfg.network.rpc_endpoint)?);
    let floor = (cfg.risk.min_balance_floor_sol * consts::LAMPORTS_PER_SOL) as u64;
    let guard = Arc::new(guard::Guard::new(floor));

    let market = market::Market::bootstrap(&cfg, &rpc, wallet, Arc::clone(&guard)).await?;
    if guard.is_tripped() {
        error!("wallet already below floor at startup");
        return Ok(consts::KILL_SWITCH_EXIT_CODE);
    }
    let route_names = Arc::new((0..market.routes.len() as u32).map(|i| market.describe_route(i)).collect::<Vec<_>>());
    let alts = load_alts(&rpc, &cfg.engine.lookup_tables).await?;
    if alts.is_empty() {
        warn!("no lookup tables configured: multi-DLMM routes will exceed tx size and be skipped");
    }

    let jito = Arc::new(jito::Jito::new(&cfg.network.jito_endpoints, cfg.network.jito_rps_per_endpoint, cfg.network.jito_uuid.clone())?);
    let tips = jito.tip_accounts().await.unwrap_or_else(|| consts::JITO_TIP_ACCOUNTS.to_vec());

    // Blockhash refresher.
    let (bh_tx, bh_rx) = tokio::sync::watch::channel((Hash::default(), Instant::now()));
    {
        let rpc = Arc::clone(&rpc);
        let every = Duration::from_millis(cfg.engine.blockhash_refresh_ms);
        tokio::spawn(async move {
            let mut t = tokio::time::interval(every);
            t.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                t.tick().await;
                match rpc.latest_blockhash().await {
                    Ok((h, _)) => {
                        let _ = bh_tx.send((h, Instant::now()));
                    }
                    Err(e) => warn!(err = %e, "blockhash refresh failed"),
                }
            }
        });
    }

    // RPC balance reconciliation (backstop for the stream-fed kill switch).
    {
        let rpc = Arc::clone(&rpc);
        let guard = Arc::clone(&guard);
        let wsol_ata = market.wsol_ata;
        let every = Duration::from_secs(cfg.engine.balance_reconcile_secs.max(1));
        tokio::spawn(async move {
            let mut t = tokio::time::interval(every);
            loop {
                t.tick().await;
                if let Ok((_, accs)) = rpc.get_multiple_accounts(&[wallet, wsol_ata], "processed").await {
                    let mut it = accs.into_iter();
                    if let Some(sol) = it.next() {
                        guard.set_sol(sol.map(|a| a.lamports).unwrap_or(0));
                    }
                    if let Some(w) = it.next() {
                        guard.set_wsol(w.filter(|a| a.data.len() >= 72).map(|a| layout::u64_at(&a.data, 64)).unwrap_or(0));
                    }
                }
            }
        });
    }

    let exec_stats = Arc::new(executor::Stats {
        opportunities: 0.into(),
        built: 0.into(),
        oversize: 0.into(),
        sim_ok: 0.into(),
        sim_fail: 0.into(),
    });
    let ingest_stats = Arc::new(ingest::IngestStats {
        updates: 0.into(),
        txs: 0.into(),
        reconnects: 0.into(),
        eval_ns_last: 0.into(),
        eval_ns_max: 0.into(),
    });

    let (exec_tx, exec_rx) = tokio::sync::mpsc::channel(256);
    let exec_cfg = executor::ExecCfg {
        cu_limit: cfg.risk.compute_unit_limit,
        cu_price: cfg.risk.compute_unit_price_micro_lamports,
        cooldown: Duration::from_millis(cfg.risk.route_cooldown_ms),
        max_age: Duration::from_millis(cfg.risk.max_opportunity_age_ms),
        dry_run: cfg.risk.dry_run,
    };
    let exec_handle = tokio::spawn(executor::run(
        exec_rx,
        Arc::clone(&kp),
        alts,
        tips,
        bh_rx,
        Arc::clone(&guard),
        Arc::clone(&jito),
        Arc::clone(&rpc),
        exec_cfg,
        Arc::clone(&exec_stats),
        route_names,
    ));
    let ingest_handle = tokio::spawn(ingest::run(Arc::clone(&cfg), market, Arc::clone(&rpc), exec_tx, Arc::clone(&ingest_stats)));

    // Periodic stats.
    {
        let (es, is, jito, guard) = (Arc::clone(&exec_stats), Arc::clone(&ingest_stats), Arc::clone(&jito), Arc::clone(&guard));
        let every = Duration::from_secs(cfg.engine.stats_interval_secs.max(1));
        tokio::spawn(async move {
            let mut t = tokio::time::interval(every);
            t.tick().await;
            loop {
                t.tick().await;
                info!(
                    updates = is.updates.swap(0, Ordering::Relaxed),
                    txs = is.txs.swap(0, Ordering::Relaxed),
                    reconnects = is.reconnects.load(Ordering::Relaxed),
                    eval_us_last = is.eval_ns_last.load(Ordering::Relaxed) / 1_000,
                    eval_us_max = is.eval_ns_max.swap(0, Ordering::Relaxed) / 1_000,
                    opps = es.opportunities.swap(0, Ordering::Relaxed),
                    built = es.built.swap(0, Ordering::Relaxed),
                    oversize = es.oversize.swap(0, Ordering::Relaxed),
                    sim_ok = es.sim_ok.swap(0, Ordering::Relaxed),
                    sim_fail = es.sim_fail.swap(0, Ordering::Relaxed),
                    bundles_ok = jito.sent.swap(0, Ordering::Relaxed),
                    bundles_err = jito.errors.swap(0, Ordering::Relaxed),
                    sol = guard.sol(),
                    wsol = guard.wsol(),
                    "stats"
                );
            }
        });
    }

    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let code = tokio::select! {
        _ = guard.notify.notified() => {
            error!("kill switch tripped; shutting down (exit {})", consts::KILL_SWITCH_EXIT_CODE);
            consts::KILL_SWITCH_EXIT_CODE
        }
        _ = tokio::signal::ctrl_c() => { info!("SIGINT; shutting down"); 0 }
        _ = sigterm.recv() => { info!("SIGTERM; shutting down"); 0 }
        r = exec_handle => { error!(?r, "executor exited"); if guard.is_tripped() { consts::KILL_SWITCH_EXIT_CODE } else { 1 } }
        r = ingest_handle => { error!(?r, "ingest exited"); if guard.is_tripped() { consts::KILL_SWITCH_EXIT_CODE } else { 1 } }
    };
    Ok(code)
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::math::dlmm::*;
    use super::math::raydium_quote;
    use solana_sdk::pubkey::Pubkey;

    #[test]
    fn keystore_roundtrip_and_wrong_passphrase() {
        use solana_sdk::signature::Keypair;
        use solana_sdk::signer::Signer;
        let dir = std::env::temp_dir().join(format!("mevks-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("key.enc");
        let _ = std::fs::remove_file(&path);
        let kp = Keypair::new();
        super::keystore::encrypt_to_file(&kp.to_bytes(), b"correct horse battery", &path).unwrap();
        let back = super::keystore::decrypt_file(&path, b"correct horse battery").unwrap();
        assert_eq!(back.pubkey(), kp.pubkey());
        assert!(super::keystore::decrypt_file(&path, b"wrong passphrase!!").is_err());
        assert!(super::keystore::encrypt_to_file(&kp.to_bytes(), b"x", &path).is_err(), "must not overwrite");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn pdas_match_mainnet() {
        assert_eq!(
            super::ixs::raydium_authority(),
            Pubkey::from_str_const("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")
        );
        assert_eq!(
            super::ixs::dlmm_event_authority(),
            Pubkey::from_str_const("D1ZN9Wj1fRSUQfCjhvnu1hqDMT7hzjzBBpi12nVniYD6")
        );
    }

    /// Worst case: 3 DLMM legs x 3 bin arrays + CU + tip. Must fit 1232 bytes once pool statics are in an ALT.
    #[test]
    fn worst_case_tx_fits_with_alt() {
        use super::consts::*;
        use super::pools::DlmmPool;
        use solana_sdk::hash::Hash;
        use solana_sdk::message::{AddressLookupTableAccount, v0};
        use solana_sdk::signature::Keypair;
        use solana_sdk::signer::Signer;
        let kp = Keypair::new();
        let me = kp.pubkey();
        let mk = || DlmmPool {
            addr: Pubkey::new_unique(),
            mint_x: Pubkey::new_unique(),
            mint_y: Pubkey::new_unique(),
            reserve_x: Pubkey::new_unique(),
            reserve_y: Pubkey::new_unique(),
            oracle: Pubkey::new_unique(),
            sp: Default::default(),
            vp: Default::default(),
            active_id: 0,
            bin_step: 10,
            status: 0,
            pair_type: 0,
            activation_type: 0,
            activation_point: 0,
            support_lo: false,
            bitmap: [0; 16],
            arrays: Default::default(),
        };
        let pools = [mk(), mk(), mk()];
        let mut ixs = vec![super::ixs::cu_limit(500_000)];
        let mut alt_keys = vec![SPL_TOKEN, super::ixs::dlmm_event_authority(), JITO_TIP_ACCOUNTS[0]];
        for p in &pools {
            let (ui, uo) = (Pubkey::new_unique(), Pubkey::new_unique());
            let arrays: Vec<Pubkey> = (0..3).map(|_| Pubkey::new_unique()).collect();
            ixs.push(super::ixs::dlmm_swap(p, ui, uo, me, &arrays, 1, 1));
            alt_keys.extend([p.addr, p.reserve_x, p.reserve_y, p.mint_x, p.mint_y, p.oracle, ui, uo]);
        }
        ixs.push(super::ixs::transfer(me, JITO_TIP_ACCOUNTS[0], 10_000));
        let no_alt = super::executor::sign_v0(&kp, v0::Message::try_compile(&me, &ixs, &[], Hash::default()).unwrap());
        let alt = AddressLookupTableAccount { key: Pubkey::new_unique(), addresses: alt_keys };
        let with_alt = super::executor::sign_v0(&kp, v0::Message::try_compile(&me, &ixs, &[alt], Hash::default()).unwrap());
        assert!(no_alt.len() > MAX_TX_SIZE, "expected oversize without ALT: {}", no_alt.len());
        assert!(with_alt.len() <= MAX_TX_SIZE, "oversize with ALT: {}", with_alt.len());
        // Signature over the exact message bytes we spliced.
        let sig = solana_sdk::signature::Signature::try_from(&with_alt[1..65]).unwrap();
        assert!(sig.verify(me.as_ref(), &with_alt[65..]));
        eprintln!("worst-case tx: {} bytes without ALT, {} with ALT", no_alt.len(), with_alt.len());
    }

    #[test]
    fn raydium_matches_reference() {
        // 1 SOL into 1000 SOL / 150k USDC pool @ 25 bps.
        let out = raydium_quote(1_000_000_000_000, 150_000_000_000, 25, 10_000, 1_000_000_000).unwrap();
        let fee = (1_000_000_000u128 * 25).div_ceil(10_000);
        let net = 1_000_000_000u128 - fee;
        assert_eq!(out as u128, 150_000_000_000u128 * net / (1_000_000_000_000u128 + net));
    }

    #[test]
    fn dlmm_pow_symmetry() {
        let p = price_from_id(100, 25).unwrap();
        let n = price_from_id(-100, 25).unwrap();
        let prod = (p >> 32) * (n >> 32);
        let one = ONE;
        assert!(prod.abs_diff(one) < one / 1_000_000);
        assert_eq!(price_from_id(0, 10).unwrap(), ONE);
    }

    #[test]
    fn mul_shr_and_shl_div_roundtrip() {
        let price = price_from_id(-1234, 10).unwrap();
        let x = 123_456_789u64;
        let y = mul_shr(price, x, false).unwrap();
        let back = shl_div(y, price, true).unwrap();
        assert!(back <= x && x - back < 2 + (ONE / price) as u64);
    }

    #[test]
    fn bitmap_scan() {
        let mut bm = [0u64; 16];
        let set = |bm: &mut [u64; 16], idx: i32| {
            let o = (idx + 512) as usize;
            bm[o / 64] |= 1 << (o % 64);
        };
        set(&mut bm, -3);
        set(&mut bm, 200);
        assert_eq!(next_liquid_array(&bm, 0, true), Some(-3));
        assert_eq!(next_liquid_array(&bm, 0, false), Some(200));
        assert_eq!(next_liquid_array(&bm, -4, true), None);
        assert_eq!(bin_array_index(-1), -1);
        assert_eq!(bin_array_index(-70), -1);
        assert_eq!(bin_array_index(-71), -2);
        assert_eq!(bin_array_index(69), 0);
    }
}
