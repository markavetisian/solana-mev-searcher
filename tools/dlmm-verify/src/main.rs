//! Executes the real Meteora lb_clmm program (LiteSVM) on real account dumps and asserts the
//! searcher's DLMM quote (`pools::DlmmPool::quote`) is lamport-exact, including the bin arrays it
//! selects for the instruction. Arg: path to a dlmm-sdk checkout (provides lb_clmm.so + dumps).
#![allow(dead_code, unused_imports)]

#[path = "../../../src/main.rs"]
mod searcher;
pub(crate) use searcher::{config, consts, executor, guard, ingest, ixs, jito, keystore, layout, market, math, pools, rpc, setup};

use base64::Engine;
use litesvm::LiteSVM;
use solana_account_v4::Account;
use solana_address_v2::Address;
use solana_clock_v3::Clock;
use solana_instruction_v3::{AccountMeta, Instruction};
use solana_keypair_v3::Keypair;
use solana_signer_v3::Signer;
use solana_transaction_v4::Transaction;
use std::path::{Path, PathBuf};

const DLMM: Address = Address::from_str_const("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo");
/// dlmm-sdk ships lb_clmm.so built with the localnet declare_id. That id is patched to the mainnet
/// id at load (single occurrence) so PDA signing matches the mainnet-derived dumped accounts.
const LOCALNET_ID: Address = Address::from_str_const("LbVRzDTvBDEcrthxfZ4RL6yiq3uZw8bS6MwtdY6UhFQ");
const PROG: Address = DLMM;
const TOKEN: Address = Address::from_str_const("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
const CB: Address = Address::from_str_const("ComputeBudget111111111111111111111111111111");
const PAIR: &str = "FJbEo74c2W4QLBBVUfUvi8VBWXtMdJVPuFpq2f6UV1iB";
const NOW: i64 = 1_760_000_000;
const SLOT: u64 = 370_000_000;
const RESERVE_BAL: u64 = 1_000_000_000_000_000;

fn a(k: &solana_sdk::pubkey::Pubkey) -> Address {
    Address::new_from_array(k.to_bytes())
}

fn load_dir(dir: &Path) -> Vec<(Address, Account)> {
    let mut out = vec![];
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        let key: Address = v["pubkey"].as_str().unwrap().parse().unwrap();
        let acc = &v["account"];
        let data = base64::engine::general_purpose::STANDARD.decode(acc["data"][0].as_str().unwrap()).unwrap();
        out.push((
            key,
            Account {
                lamports: acc["lamports"].as_u64().unwrap(),
                data,
                owner: acc["owner"].as_str().unwrap().parse().unwrap(),
                executable: false,
                rent_epoch: 0,
            },
        ));
    }
    out
}

/// Rewrite the declared program id inside an SBF ELF: the 32-byte constant blob plus every
/// `lddw` (opcode 0x18) immediate pair that materialises one of its four u64 limbs.
fn patch_program_id(so: &mut [u8], from: &[u8; 32], to: &[u8; 32]) {
    let blob: Vec<usize> = so.windows(32).enumerate().filter(|(_, w)| *w == from).map(|(i, _)| i).collect();
    for i in &blob {
        so[*i..*i + 32].copy_from_slice(to);
    }
    let mut per_limb = [0usize; 4];
    for limb in 0..4 {
        let (f, t) = (&from[8 * limb..8 * limb + 8], &to[8 * limb..8 * limb + 8]);
        let mut j = 4;
        while j + 12 <= so.len() {
            if so[j - 4] == 0x18 && so[j..j + 4] == f[..4] && so[j + 4..j + 8] == [0; 4] && so[j + 8..j + 12] == f[4..] {
                so[j..j + 4].copy_from_slice(&t[..4]);
                so[j + 8..j + 12].copy_from_slice(&t[4..]);
                per_limb[limb] += 1;
                j += 12;
            } else {
                j += 1;
            }
        }
    }
    assert!(per_limb.iter().all(|&n| n == per_limb[0] && n > 0), "inconsistent lddw patch {per_limb:?}");
    eprintln!("patched declare_id: blob x{}, lddw x{} per limb", blob.len(), per_limb[0]);
}

fn token_acc(mint: &Address, owner: &Address, amount: u64) -> Account {
    let mut d = vec![0u8; 165];
    d[0..32].copy_from_slice(mint.as_ref());
    d[32..64].copy_from_slice(owner.as_ref());
    d[64..72].copy_from_slice(&amount.to_le_bytes());
    d[108] = 1;
    Account { lamports: 10_000_000, data: d, owner: TOKEN, executable: false, rent_epoch: 0 }
}

fn oracle_acc() -> Account {
    let len = 100u64;
    let mut d = Vec::with_capacity(32 + 32 * len as usize);
    d.extend_from_slice(&[139, 194, 131, 179, 140, 179, 229, 244]);
    d.extend_from_slice(&0u64.to_le_bytes());
    d.extend_from_slice(&0u64.to_le_bytes());
    d.extend_from_slice(&len.to_le_bytes());
    d.resize(32 + 32 * len as usize, 0);
    Account { lamports: 100_000_000, data: d, owner: PROG, executable: false, rent_epoch: 0 }
}

struct Variant {
    name: &'static str,
    patch: fn(&mut [u8]),
}

fn w16(d: &mut [u8], o: usize, v: u16) {
    d[o..o + 2].copy_from_slice(&v.to_le_bytes());
}
fn w32(d: &mut [u8], o: usize, v: u32) {
    d[o..o + 4].copy_from_slice(&v.to_le_bytes());
}
fn wi32(d: &mut [u8], o: usize, v: i32) {
    d[o..o + 4].copy_from_slice(&v.to_le_bytes());
}
fn wi64(d: &mut [u8], o: usize, v: i64) {
    d[o..o + 8].copy_from_slice(&v.to_le_bytes());
}

fn onchain_swap(
    so: &[u8],
    base: &[(Address, Account)],
    pair_key: Address,
    pair_bytes: &[u8],
    p: &pools::DlmmPool,
    sfy: bool,
    amount: u64,
    arrays: &[Address],
) -> Result<u64, String> {
    let mut svm = LiteSVM::new();
    svm.add_program(PROG, so).map_err(|e| format!("{e:?}"))?;
    let mut clock: Clock = svm.get_sysvar();
    clock.unix_timestamp = NOW;
    clock.slot = SLOT;
    svm.set_sysvar(&clock);
    let remap_owner = |mut acc: Account| {
        if acc.owner == DLMM {
            acc.owner = PROG;
        }
        acc
    };
    for (k, acc) in base {
        svm.set_account(*k, remap_owner(acc.clone())).unwrap();
    }
    let mut pair_acc = remap_owner(base.iter().find(|(k, _)| *k == pair_key).unwrap().1.clone());
    pair_acc.data = pair_bytes.to_vec();
    svm.set_account(pair_key, pair_acc).unwrap();
    let (mx, my) = (a(&p.mint_x), a(&p.mint_y));
    svm.set_account(a(&p.reserve_x), token_acc(&mx, &pair_key, RESERVE_BAL)).unwrap();
    svm.set_account(a(&p.reserve_y), token_acc(&my, &pair_key, RESERVE_BAL)).unwrap();
    svm.set_account(a(&p.oracle), oracle_acc()).unwrap();

    let user = Keypair::new();
    svm.airdrop(&user.pubkey(), 10_000_000_000).unwrap();
    let ux = Address::new_unique();
    let uy = Address::new_unique();
    svm.set_account(ux, token_acc(&mx, &user.pubkey(), RESERVE_BAL)).unwrap();
    svm.set_account(uy, token_acc(&my, &user.pubkey(), RESERVE_BAL)).unwrap();
    let (uin, uout) = if sfy { (ux, uy) } else { (uy, ux) };

    // Same account list as ixs::dlmm_swap.
    let real = ixs::dlmm_swap(
        p,
        solana_sdk::pubkey::Pubkey::new_from_array(uin.to_bytes()),
        solana_sdk::pubkey::Pubkey::new_from_array(uout.to_bytes()),
        solana_sdk::pubkey::Pubkey::new_from_array(user.pubkey().to_bytes()),
        &arrays.iter().map(|k| solana_sdk::pubkey::Pubkey::new_from_array(k.to_bytes())).collect::<Vec<_>>(),
        amount,
        0,
    );
    let ev_mainnet = Address::new_from_array(ixs::dlmm_event_authority().to_bytes());
    let ev_local = Address::find_program_address(&[b"__event_authority"], &PROG).0;
    let remap = |k: Address| if k == DLMM { PROG } else if k == ev_mainnet { ev_local } else { k };
    let ix = Instruction {
        program_id: PROG,
        accounts: real
            .accounts
            .iter()
            .map(|m| AccountMeta { pubkey: remap(Address::new_from_array(m.pubkey.to_bytes())), is_signer: m.is_signer, is_writable: m.is_writable })
            .collect(),
        data: real.data.clone(),
    };
    let mut cu = vec![2u8];
    cu.extend_from_slice(&1_400_000u32.to_le_bytes());
    let cu_ix = Instruction { program_id: CB, accounts: vec![], data: cu };
    let tx = Transaction::new_signed_with_payer(&[cu_ix, ix], Some(&user.pubkey()), &[&user], svm.latest_blockhash());
    match svm.send_transaction(tx) {
        Ok(_) => {
            if std::env::var("VERBOSE").is_ok() {
                let pd = svm.get_account(&pair_key).unwrap().data;
                let r = |o: usize| i64::from_le_bytes(pd[o..o + 8].try_into().unwrap());
                let c: Clock = svm.get_sysvar();
                eprintln!(
                    "post: clock.ts={} va={} vr={} iref={} last_ts={} (now-last={})",
                    c.unix_timestamp,
                    u32::from_le_bytes(pd[40..44].try_into().unwrap()),
                    u32::from_le_bytes(pd[44..48].try_into().unwrap()),
                    i32::from_le_bytes(pd[48..52].try_into().unwrap()),
                    r(56),
                    c.unix_timestamp - r(56)
                );
            }
            let acc = svm.get_account(&uout).unwrap();
            let bal = u64::from_le_bytes(acc.data[64..72].try_into().unwrap());
            Ok(bal - RESERVE_BAL)
        }
        Err(e) => {
            if std::env::var("VERBOSE").is_ok() {
                eprintln!("---- {:?}\n{}", e.err, e.meta.logs.join("\n"));
            }
            Err(format!("{:?}", e.err))
        }
    }
}

fn main() {
    let sdk = PathBuf::from(std::env::args().nth(1).expect("usage: dlmm-verify <dlmm-sdk dir>"));
    let mut so = std::fs::read(sdk.join("artifacts/lb_clmm.so")).expect("lb_clmm.so");
    patch_program_id(&mut so, LOCALNET_ID.as_ref().try_into().unwrap(), DLMM.as_ref().try_into().unwrap());
    let base = load_dir(&sdk.join("artifacts").join(PAIR));
    let pair_key: Address = PAIR.parse().unwrap();
    let pair_raw = base.iter().find(|(k, _)| *k == pair_key).unwrap().1.data.clone();

    let variants = [
        Variant { name: "as-dumped (fee capped 10%)", patch: |_| {} },
        Variant { name: "base 0.1% + variable fee", patch: |d| d[34] = 0 },
        Variant { name: "fee on Y only", patch: |d| { d[34] = 0; d[36] = 1; } },
        // NOTE: the dlmm-sdk lb_clmm.so is a localnet build that hard-codes filter=5s / decay=10s
        // (stored per-pair periods are ignored; verified by sweeping). Mainnet uses the stored
        // values, as the SDK quote does. So only the regimes where both rules agree are asserted:
        // elapsed < 5s (references kept) and elapsed >= stored decay (full reset).
        Variant {
            name: "stale refs, full reset (elapsed >= decay)",
            patch: |d| { d[34] = 0; w32(d, 40, 120_000); w32(d, 44, 40_000); wi32(d, 48, -5); wi64(d, 56, NOW - 5_000); },
        },
        Variant {
            name: "high-frequency, refs kept (elapsed 3s)",
            patch: |d| { d[34] = 0; w32(d, 40, 90_000); w32(d, 44, 30_000); wi32(d, 48, 7); wi64(d, 56, NOW - 3); },
        },
        Variant {
            name: "high-frequency, va near cap",
            patch: |d| { d[34] = 0; w32(d, 40, 340_000); w32(d, 44, 300_000); wi32(d, 48, -12); wi64(d, 56, NOW - 1); },
        },
        Variant { name: "limit-order fn type", patch: |d| { d[34] = 0; d[35] = 2; } },
        Variant { name: "bin_step 25 / factor 8000", patch: |d| { d[34] = 0; w16(d, 8, 8_000); } },
    ];
    // 60 geometric points 1 .. 1e13 (+ odd offsets so rounding paths get exercised).
    let amounts: Vec<u64> = (0..60).map(|i| (10f64.powf(i as f64 * 13.0 / 59.0) as u64).max(1) + (i as u64 % 7)).collect();

    let (mut ok, mut both_fail, mut bad, mut multi_bin) = (0, 0, 0, 0);
    for v in &variants {
        let mut d = pair_raw.clone();
        (v.patch)(&mut d);
        let pk = solana_sdk::pubkey::Pubkey::new_from_array(pair_key.to_bytes());
        let mut p = pools::DlmmPool::parse(pk, &d).expect("parse");
        // Attach bin arrays at the PDAs the searcher derives; verify they match the dump keys.
        let mut idxs = vec![];
        p.liquid_arrays(true, 8, &mut idxs);
        p.liquid_arrays(false, 8, &mut idxs);
        idxs.sort();
        idxs.dedup();
        for idx in &idxs {
            let key = pools::bin_array_pda(&pk, *idx);
            let acc = base.iter().find(|(k, _)| k.to_bytes() == key.to_bytes()).unwrap_or_else(|| panic!("no dump for derived bin array {idx} {key}"));
            p.arrays.insert(*idx, pools::BinArraySlot { key, data: Some(acc.1.data.clone()) });
        }
        println!("== {} (arrays {:?}, active {}, step {})", v.name, idxs, p.active_id, p.bin_step);
        for sfy in [true, false] {
            for &amt in &amounts {
                let mut used = vec![];
                let mine = p.quote(amt, sfy, NOW, SLOT, 8, Some(&mut used));
                let arrays: Vec<Address> = if mine.is_some() {
                    used.iter().map(|i| a(&p.arrays[i].key)).collect()
                } else {
                    let mut all = vec![];
                    p.liquid_arrays(sfy, 8, &mut all);
                    all.iter().map(|i| a(&p.arrays[i].key)).collect()
                };
                let chain = onchain_swap(&so, &base, pair_key, &d, &p, sfy, amt, &arrays);
                let verdict = match (&mine, &chain) {
                    (Some(m), Ok(c)) if m == c => { ok += 1; if used.len() > 1 { multi_bin += 1; } "OK" }
                    (None, Err(_)) => { both_fail += 1; "ok(both reject)" }
                    _ => { bad += 1; "MISMATCH" }
                };
                println!(
                    "  sfy={sfy:<5} in={amt:<16} mine={:<22} chain={:<40} arrays={:?} {verdict}",
                    format!("{mine:?}"),
                    match &chain { Ok(c) => format!("Ok({c})"), Err(e) => e.chars().take(38).collect() },
                    used
                );
            }
        }
    }
    println!("\nexact={ok} (multi-array={multi_bin}) both_reject={both_fail} MISMATCH={bad}");
    if bad > 0 {
        std::process::exit(1);
    }
}
