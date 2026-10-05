# solana-mev-searcher

Cross-DEX atomic triangular arbitrage searcher: **Raydium AMM v4 + Meteora DLMM**, 3-leg wSOL cycles, one atomic v0 transaction per opportunity (3 swaps + Jito tip), submitted as a Jito bundle.

```
Yellowstone gRPC ──► ingest/engine task ──(mpsc)──► executor task ──► Jito block engines (all regions)
     ▲   zero-copy parse, route matrix, sizing      kill switch, v0+ALT compile, sign once
     └── RPC snapshots (bootstrap / reconnect resync / newly tracked DLMM bin arrays)
```

All code is in `src/main.rs` (modules: `layout`, `math`, `pools`, `market`, `ingest`, `executor`, `jito`, `guard`, `keystore`, `setup`).

## Deploy (bare-metal Linux)

```bash
cargo build --release                       # toolchain pinned in rust-toolchain.toml
sudo install -m 755 target/release/searcher /usr/local/bin/
sudo useradd -r -s /usr/sbin/nologin mev && sudo mkdir -p /etc/mev-searcher

# 1. Keystore (argon2id 64 MiB + XChaCha20-Poly1305, written 0600, never overwrites)
searcher encrypt-key --input ~/id.json --output /etc/mev-searcher/key.enc && shred -u ~/id.json
echo -n 'your passphrase' | sudo tee /etc/mev-searcher/passphrase >/dev/null && sudo chmod 600 /etc/mev-searcher/passphrase
echo 'GRPC_X_TOKEN=...' | sudo tee /etc/mev-searcher/env >/dev/null && sudo chmod 600 /etc/mev-searcher/env

# 2. Config
sudo cp config.example.toml /etc/mev-searcher/config.toml   # fill endpoints + pools

# 3. One-time wallet setup: ATAs for every pool mint, wrap trading capital, build the ALT
MEV_KEY_PASSPHRASE=... searcher setup -c /etc/mev-searcher/config.toml --wrap-sol 5 --create-alt
#    -> paste the printed table into [engine].lookup_tables

# 4. Run (dry_run = true first: every opportunity is simulateTransaction'd and logged, nothing sent)
sudo cp deploy/mev-searcher.service /etc/systemd/system/ && sudo systemctl enable --now mev-searcher
journalctl -u mev-searcher -f
```

Go live by setting `risk.dry_run = false` and `systemctl restart mev-searcher`.

## Execution guarantees

| Guard | Mechanism |
|---|---|
| Never lands at a loss | Leg-3 `min_out = max(sim × (1 − slippage_bps), amount_in + tip + fee + min_profit)`. Any shortfall reverts the whole tx; a reverted tx can't land inside a bundle, so the tip is never paid. |
| 0.5% slippage | Same leg-3 floor (`slippage_bps = 50`). Intermediate legs: `min_out` = exact input of the next leg (minus a 1 bp rounding haircut). |
| Tip only on success | Tip is a `SystemProgram::Transfer` inside the same tx (last ix), so unbundling or uncle-bandit replay can't extract it. |
| Kill switch | Wallet + wSOL ATA are streamed over the same gRPC subscription (zero-latency balances), reconciled by RPC every 5s, checked before every tx build. `SOL + wSOL < min_balance_floor_sol` latches, halts the executor and exits **42**. The systemd unit has `RestartPreventExitStatus=42`, so it stays down. |
| Stale state | Per-account `(slot, write_version)` ordering; trading suspended after a reconnect until the RPC resnapshot lands; opportunities older than 150 ms are dropped; blockhash older than 20s blocks sending. |
| Reconnect | HTTP/2 keepalive at 1s with a 2s timeout, plus a 3s stream-silence watchdog (slots arrive every ~400 ms). The first retry fires after 50 ms, then backs off exponentially to a 2s cap. |
| Torn reads | Each gRPC burst is fully drained before evaluating. A Raydium pool is only priced when both vaults carry the same tx signature. |

## Verification

* `cargo test --release`: math invariants, mainnet PDA constants, keystore round-trip and wrong-passphrase rejection, and the worst-case tx-size check (3 DLMM legs × 3 bin arrays: 1544 B without an ALT, 741 B with one).
* `tools/dlmm-verify`: runs Meteora's real `lb_clmm.so` in LiteSVM on real account dumps and asserts the searcher's DLMM quote and bin-array selection are **lamport-exact**. 7 fee/volatility/fee-mode/limit-order variants × both directions × 60 sizes gave 544 exact matches (231 crossing several bin arrays) and 0 mismatches.
  ```bash
  git clone --depth 1 https://github.com/MeteoraAg/dlmm-sdk /tmp/dlmm-sdk
  cargo run --release --ignore-rust-version --manifest-path tools/dlmm-verify/Cargo.toml -- /tmp/dlmm-sdk
  ```
  The SDK ships a localnet build of the program, with filter/decay hard-coded to 5s/10s. The harness patches its declared program ID to mainnet and only asserts volatility regimes where that build and mainnet agree.

## Scope / limits

* SPL-Token pools only. Token-2022 DLMM legs need `swap2` and are skipped. DLMM pairs trading outside the ±512 internal bitmap range (which need the bitmap extension account) are skipped.
* Raydium legs use `SwapBaseInV2` (tag 16, 8 accounts), the orderbook-free path that has been canonical since OpenBook was removed from AMM v4.
* Jito's unauthenticated limit is about 1 req/s per region. Get a `jito_uuid` for more throughput.
