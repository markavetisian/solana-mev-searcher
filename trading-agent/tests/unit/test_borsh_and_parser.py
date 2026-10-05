from __future__ import annotations

import base64

import pytest

from helpers import key
from tradingagent.common.types import EventKind, Side
from tradingagent.ingestion.parser import AMM, PUMP, LogParser, parse_logs_notification
from tradingagent.pump.borsh import EVENT_IX_TAG, BorshError, amm_codec, anchor_discriminator, pump_codec

DEFAULT = "11111111111111111111111111111111"


def trade_event(mint: str, is_buy: bool = True, **over) -> dict:
    v = {
        "mint": mint,
        "sol_amount": 1_000_000_000,
        "token_amount": 30_000_000_000_000,
        "is_buy": is_buy,
        "user": key("u"),
        "timestamp": 1_760_000_000,
        "virtual_sol_reserves": 31_000_000_000,
        "virtual_token_reserves": 1_043_000_000_000_000,
        "real_sol_reserves": 1_000_000_000,
        "real_token_reserves": 763_100_000_000_000,
        "fee_recipient": key("fee"),
        "fee_basis_points": 93,
        "fee": 9_300_000,
        "creator": key("c"),
        "creator_fee_basis_points": 30,
        "creator_fee": 3_000_000,
        "track_volume": True,
        "total_unclaimed_tokens": 0,
        "total_claimed_tokens": 0,
        "current_sol_volume": 0,
        "last_update_timestamp": 0,
        "ix_name": "buy_exact_quote_in_v2",
        "mayhem_mode": False,
        "cashback_fee_basis_points": 0,
        "cashback": 0,
        "buyback_fee_basis_points": 0,
        "buyback_fee": 0,
        "shareholders": [{"address": key("s"), "share_bps": 10000}],
        "quote_mint": DEFAULT,
        "quote_amount": 1_000_000_000,
        "virtual_quote_reserves": 31_000_000_000,
        "real_quote_reserves": 1_000_000_000,
        "holder_rewards_bps": 0,
        "holder_rewards": 0,
    }
    v.update(over)
    return v


def b64_event(name: str, values: dict, codec=None) -> str:
    return base64.b64encode((codec or pump_codec()).encode_event(name, values)).decode()


def test_discriminators_match_anchor_convention():
    c = pump_codec()
    assert anchor_discriminator("event", "TradeEvent") in c.events
    assert c.events[anchor_discriminator("event", "TradeEvent")] == "TradeEvent"
    assert EVENT_IX_TAG == anchor_discriminator("anchor", "event")


def test_trade_event_roundtrip_including_vec_and_string():
    mint = key("m")
    raw = pump_codec().encode_event("TradeEvent", trade_event(mint))
    name, ev = pump_codec().decode_event(raw)
    assert name == "TradeEvent" and ev["mint"] == mint and ev["ix_name"] == "buy_exact_quote_in_v2"
    assert ev["shareholders"][0]["share_bps"] == 10000
    # emit_cpi framing decodes identically
    assert pump_codec().decode_event(EVENT_IX_TAG + raw)[1] == ev


def test_older_shorter_events_decode_with_defaults():
    """Pump appends fields; data written before a field existed is shorter. Missing trailing fields = defaults."""
    c = pump_codec()
    full = c.encode_event("TradeEvent", trade_event(key("m")))
    cut = len(full) - (8 + 8)  # drop holder_rewards_bps + holder_rewards
    name, ev = c.decode_event(full[:cut])
    assert ev["holder_rewards"] == 0 and ev["_missing"] == ["holder_rewards_bps", "holder_rewards"]


def test_truncation_inside_a_field_is_malformed():
    c = pump_codec()
    full = c.encode_event("TradeEvent", trade_event(key("m")))
    with pytest.raises(BorshError):
        c.decode_event(full[:-3])


def _logs(*data_lines: str, program: str = PUMP) -> list[str]:
    return [
        "Program ComputeBudget111111111111111111111111111111 invoke [1]",
        "Program ComputeBudget111111111111111111111111111111 success",
        f"Program {program} invoke [1]",
        "Program log: Instruction: Buy",
        *[f"Program data: {d}" for d in data_lines],
        f"Program {program} invoke [2]",
        f"Program {program} consumed 2000 of 3000 compute units",
        f"Program {program} success",
        f"Program {program} consumed 50000 of 150000 compute units",
        f"Program {program} success",
    ]


def test_parser_decodes_trade_from_logs():
    mint = key("m")
    p = LogParser()
    res = p.parse("sig1", 123, _logs(b64_event("TradeEvent", trade_event(mint))), None, 1000.0)
    assert len(res.events) == 1
    ev = res.events[0]
    assert ev.kind is EventKind.TRADE and ev.side is Side.BUY and ev.mint == mint and ev.slot == 123
    assert ev.sol_amount == 1_000_000_000 and ev.fee_lamports == 12_300_000
    assert ev.virtual_quote_reserves == 31_000_000_000


def test_parser_skips_failed_transactions():
    res = LogParser().parse(
        "sig",
        1,
        _logs(b64_event("TradeEvent", trade_event(key("m")))),
        {"InstructionError": [2, {"Custom": 6002}]},
        1.0,
    )
    assert res.failed_tx and res.events == []


def test_parser_ignores_data_from_other_programs():
    other = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    res = LogParser().parse("sig", 1, _logs(b64_event("TradeEvent", trade_event(key("m"))), program=other), None, 1.0)
    assert res.events == []


def test_parser_malformed_payloads_produce_no_events():
    res = LogParser().parse(
        "sig",
        1,
        _logs("!!!not-base64!!!", base64.b64encode(b"\x00" * 40).decode(), base64.b64encode(b"\x01").decode()),
        None,
        1.0,
    )
    assert res.events == []


def test_truncated_logs_emit_data_gap():
    logs = _logs(b64_event("TradeEvent", trade_event(key("m")))) + ["Log truncated"]
    res = LogParser().parse("sig", 7, logs, None, 1.0)
    assert res.truncated and res.events[-1].kind is EventKind.DATA_GAP


def test_amm_buy_event_resolves_pool_and_reconstructs_post_reserves():
    pool, mint = key("pool"), key("mint")
    amm = amm_codec()
    vals = {f["name"]: 0 for f in amm.types["BuyEvent"]["type"]["fields"]}
    vals.update(
        {
            k: key(k)
            for k in (
                "pool",
                "user",
                "user_base_token_account",
                "user_quote_token_account",
                "protocol_fee_recipient",
                "protocol_fee_recipient_token_account",
                "coin_creator",
            )
        }
    )
    vals.update(
        {
            "pool": pool,
            "timestamp": 5,
            "base_amount_out": 1_000,
            "pool_base_token_reserves": 1_000_000,
            "pool_quote_token_reserves": 50_000,
            "quote_amount_in": 52,
            "lp_fee": 1,
            "ix_name": "buy",
            "track_volume": False,
            "can_boost": False,
        }
    )
    p = LogParser({pool: mint})
    res = p.parse("s", 1, _logs(b64_event("BuyEvent", vals, amm), program=AMM), None, 1.0)
    ev = res.events[0]
    assert ev.kind is EventKind.AMM_TRADE and ev.mint == mint
    assert ev.pool_base_reserves == 999_000 and ev.pool_quote_reserves == 50_053


def test_logs_notification_envelope():
    msg = {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": 9},
                "value": {
                    "signature": "abc",
                    "err": None,
                    "logs": _logs(b64_event("TradeEvent", trade_event(key("m")))),
                },
            }
        },
    }
    res = parse_logs_notification(LogParser(), msg, 2.0)
    assert res is not None and res.events[0].slot == 9 and res.events[0].observed_at == 2.0
