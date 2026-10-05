"""IDL-driven Borsh decoder for Anchor accounts and events.

The decoder reads the official Pump IDL JSON (vendored under pump/idl, see versions.PUMP_IDL_COMMIT) instead of
hand-written struct offsets, so a layout change is an IDL refresh, not a code rewrite.

Pump appends fields to accounts and events over time and older data is shorter than the current layout. The
documented contract is "read missing trailing fields as 0 / false / default". `decode_struct(..., tolerant=True)`
implements exactly that: when the buffer ends on a field boundary, all remaining fields are filled with their
type default and the field names are reported in `_missing`. A buffer that ends in the *middle* of a field is
malformed and raises.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from functools import lru_cache
from pathlib import Path
from typing import Any

from solders.pubkey import Pubkey

IDL_DIR = Path(__file__).parent / "idl"


class BorshError(ValueError):
    pass


class _Truncated(BorshError):
    """Buffer ended exactly at a field boundary (legitimate for appended fields)."""


@lru_cache(maxsize=8)
def load_idl(name: str) -> dict:
    return json.loads((IDL_DIR / f"{name}.json").read_text())


def anchor_discriminator(namespace: str, name: str) -> bytes:
    return hashlib.sha256(f"{namespace}:{name}".encode()).digest()[:8]


# Anchor emit_cpi! wraps events in a self-CPI whose data starts with this tag.
EVENT_IX_TAG = bytes.fromhex("1d9acb512ea545e4")  # sha256("anchor:event")[:8]


class Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            if self.pos == len(self.buf):
                raise _Truncated("end of buffer")
            raise BorshError(f"buffer underrun: need {n} at {self.pos}, have {len(self.buf) - self.pos}")
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    @property
    def remaining(self) -> int:
        return len(self.buf) - self.pos


_PRIMS = {
    "u8": ("<B", 1),
    "i8": ("<b", 1),
    "u16": ("<H", 2),
    "i16": ("<h", 2),
    "u32": ("<I", 4),
    "i32": ("<i", 4),
    "u64": ("<Q", 8),
    "i64": ("<q", 8),
}


class IdlCodec:
    def __init__(self, idl: dict) -> None:
        self.idl = idl
        self.types = {t["name"]: t for t in idl.get("types", [])}
        self.accounts = {bytes(a["discriminator"]): a["name"] for a in idl.get("accounts", [])}
        self.events = {bytes(e["discriminator"]): e["name"] for e in idl.get("events", [])}
        self.instructions = {i["name"]: i for i in idl.get("instructions", [])}
        self.address = idl.get("address")

    # ---- primitives -------------------------------------------------------------------------------------
    def _read(self, r: Reader, ty: Any) -> Any:
        if isinstance(ty, str):
            if ty in _PRIMS:
                fmt, n = _PRIMS[ty]
                return struct.unpack(fmt, r.take(n))[0]
            if ty == "bool":
                b = r.take(1)[0]
                if b > 1:
                    raise BorshError(f"invalid bool byte {b}")
                return b == 1
            if ty == "u128":
                return int.from_bytes(r.take(16), "little", signed=False)
            if ty == "i128":
                return int.from_bytes(r.take(16), "little", signed=True)
            if ty == "pubkey":
                return str(Pubkey.from_bytes(r.take(32)))
            if ty == "string":
                n = struct.unpack("<I", r.take(4))[0]
                if n > r.remaining:
                    raise BorshError("string length exceeds buffer")
                return r.take(n).decode("utf-8", errors="replace")
            if ty == "bytes":
                n = struct.unpack("<I", r.take(4))[0]
                return r.take(n)
            raise BorshError(f"unsupported primitive {ty}")
        if "vec" in ty:
            n = struct.unpack("<I", r.take(4))[0]
            if n > 100_000:
                raise BorshError("vec too long")
            return [self._read(r, ty["vec"]) for _ in range(n)]
        if "array" in ty:
            inner, n = ty["array"]
            return [self._read(r, inner) for _ in range(n)]
        if "option" in ty:
            tag = r.take(1)[0]
            if tag == 0:
                return None
            if tag != 1:
                raise BorshError("invalid option tag")
            return self._read(r, ty["option"])
        if "defined" in ty:
            name = ty["defined"]["name"] if isinstance(ty["defined"], dict) else ty["defined"]
            return self._read_defined(r, name, tolerant=False)
        raise BorshError(f"unsupported type {ty}")

    def _default(self, ty: Any) -> Any:
        if isinstance(ty, str):
            if ty == "bool":
                return False
            if ty == "pubkey":
                return "11111111111111111111111111111111"
            if ty == "string":
                return ""
            return 0
        if "vec" in ty:
            return []
        if "array" in ty:
            inner, n = ty["array"]
            return [self._default(inner) for _ in range(n)]
        if "option" in ty:
            return None
        return None

    def _read_defined(self, r: Reader, name: str, tolerant: bool) -> Any:
        t = self.types.get(name)
        if t is None:
            raise BorshError(f"unknown type {name}")
        body = t["type"]
        if body["kind"] == "struct":
            fields = body.get("fields", [])
            if fields and not isinstance(fields[0], dict):  # tuple struct, e.g. OptionBool(bool)
                return [self._read(r, f) for f in fields]
            return self._read_fields(r, fields, tolerant)
        if body["kind"] == "enum":
            idx = r.take(1)[0]
            variants = body["variants"]
            if idx >= len(variants):
                raise BorshError(f"enum {name} variant {idx} out of range")
            v = variants[idx]
            if v.get("fields"):
                fields = v["fields"]
                if isinstance(fields[0], dict):
                    return {v["name"]: self._read_fields(r, fields, False)}
                return {v["name"]: [self._read(r, f) for f in fields]}
            return v["name"]
        raise BorshError(f"unsupported kind {body['kind']}")

    def _read_fields(self, r: Reader, fields: list[dict], tolerant: bool) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i, f in enumerate(fields):
            try:
                out[f["name"]] = self._read(r, f["type"])
            except _Truncated:
                if not tolerant:
                    raise BorshError(f"truncated before field {f['name']}") from None
                missing = [ff["name"] for ff in fields[i:]]
                for ff in fields[i:]:
                    out[ff["name"]] = self._default(ff["type"])
                out["_missing"] = missing
                return out
        return out

    # ---- public API -------------------------------------------------------------------------------------
    def decode_struct(self, name: str, data: bytes, tolerant: bool = True) -> dict[str, Any]:
        return self._read_defined(Reader(data), name, tolerant=tolerant)

    def decode_account(self, data: bytes, expected: str | None = None) -> tuple[str, dict[str, Any]]:
        if len(data) < 8:
            raise BorshError("account data shorter than discriminator")
        name = self.accounts.get(bytes(data[:8]))
        if name is None:
            raise BorshError("unknown account discriminator")
        if expected and name != expected:
            raise BorshError(f"expected {expected}, got {name}")
        return name, self.decode_struct(name, data[8:])

    def decode_event(self, data: bytes) -> tuple[str, dict[str, Any]] | None:
        """Decode an Anchor event from a `Program data:` payload or an emit_cpi instruction payload."""
        if data[:8] == EVENT_IX_TAG:
            data = data[8:]
        if len(data) < 8:
            return None
        name = self.events.get(bytes(data[:8]))
        if name is None:
            return None
        return name, self.decode_struct(name, data[8:])

    def decode_event_b64(self, b64: str) -> tuple[str, dict[str, Any]] | None:
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            return None
        return self.decode_event(raw)

    def instruction_discriminator(self, name: str) -> bytes:
        return bytes(self.instructions[name]["discriminator"])

    def instruction_accounts(self, name: str) -> list[dict]:
        return self.instructions[name]["accounts"]

    # ---- encoding (tests / synthetic data only) ---------------------------------------------------------
    def encode_struct(self, name: str, values: dict[str, Any]) -> bytes:
        body = self.types[name]["type"]
        return b"".join(self._enc(f["type"], values[f["name"]]) for f in body["fields"])

    def _enc(self, ty: Any, v: Any) -> bytes:
        if isinstance(ty, str):
            if ty in _PRIMS:
                return struct.pack(_PRIMS[ty][0], v)
            if ty == "bool":
                return b"\x01" if v else b"\x00"
            if ty == "u128":
                return int(v).to_bytes(16, "little", signed=False)
            if ty == "i128":
                return int(v).to_bytes(16, "little", signed=True)
            if ty == "pubkey":
                return bytes(Pubkey.from_string(v))
            if ty == "string":
                b = v.encode()
                return struct.pack("<I", len(b)) + b
        elif "vec" in ty:
            return struct.pack("<I", len(v)) + b"".join(self._enc(ty["vec"], x) for x in v)
        elif "array" in ty:
            return b"".join(self._enc(ty["array"][0], x) for x in v)
        elif "option" in ty:
            return b"\x00" if v is None else b"\x01" + self._enc(ty["option"], v)
        elif "defined" in ty:
            name = ty["defined"]["name"] if isinstance(ty["defined"], dict) else ty["defined"]
            body = self.types[name]["type"]
            if body["kind"] == "struct":
                if body["fields"] and not isinstance(body["fields"][0], dict):
                    return b"".join(self._enc(f, x) for f, x in zip(body["fields"], v, strict=True))
                return self.encode_struct(name, v)
            if body["kind"] == "enum":
                names = [x["name"] for x in body["variants"]]
                return bytes([names.index(v)])
        raise BorshError(f"cannot encode {ty}")

    def encode_event(self, name: str, values: dict[str, Any]) -> bytes:
        disc = next(d for d, n in self.events.items() if n == name)
        return disc + self.encode_struct(name, values)


@lru_cache(maxsize=4)
def codec(name: str) -> IdlCodec:
    return IdlCodec(load_idl(name))


def pump_codec() -> IdlCodec:
    return codec("pump")


def amm_codec() -> IdlCodec:
    return codec("pump_amm")
