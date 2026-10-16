"""Post-login S400 session: notification wiring, CMTP reassembly and frame parsing.

Frame format and parser adapted from nokistin/xiaomi-s400-live (Apache-2.0).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .auth import NotifyHub, login
from .crypto import SessionKeys, decrypt_cmtp
from .protocol import AVCTP, AVDTP, CMTP, RCV_OK, RCV_RDY, UPNP, VEND1A, VEND1C

_LOGGER = logging.getLogger(__name__)

NOTIFY_UUIDS = (UPNP, AVDTP, AVCTP, VEND1A, CMTP, VEND1C)
_DISCONNECTED = b""


@dataclass
class ScaleEvent:
    """A decoded frame from the scale."""

    type: str  # "live" | "final" | "raw"
    weight_kg: float | None = None
    stable: bool = False
    impedance_ohm: float | None = None
    impedance_low_ohm: float | None = None
    profile_id: int | None = None
    timestamp: int | None = None
    fields: list[str] = field(default_factory=list)
    plaintext: bytes = b""


def _int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else None


def _div10(value: int | None) -> float | None:
    return value / 10 if value is not None else None


def parse_payload(plaintext: bytes) -> ScaleEvent:
    """Parse decrypted CMTP plaintext.

    Observed layout (yunmai.scales.ms104):
        LL 20 CC 00 07 08 PP 00 01 PP 00 SS a0 <ascii csv>
    Live  (2 fields):  "weight_x10,stable"
    Final (32 fields): "0,0,profile,weight_x10,stable,2,unix_ts,0..0,imp_x10,imp_low_x10"
    """
    idx = plaintext.find(b"\xa0")
    if idx < 0:
        return ScaleEvent(type="raw", plaintext=plaintext)
    text = plaintext[idx + 1 :].decode("ascii", errors="replace").rstrip("\x00").strip()
    parts = text.split(",")

    if len(parts) == 2:
        return ScaleEvent(
            type="live",
            weight_kg=_div10(_int(parts[0])),
            stable=parts[1].strip() == "1",
            fields=parts,
            plaintext=plaintext,
        )
    if len(parts) >= 8:
        imp = _int(parts[-2])
        imp_low = _int(parts[-1])
        return ScaleEvent(
            type="final",
            profile_id=_int(parts[2]),
            weight_kg=_div10(_int(parts[3])),
            stable=parts[4].strip() == "1",
            timestamp=_int(parts[6]),
            impedance_ohm=_div10(imp) if imp else None,
            impedance_low_ohm=_div10(imp_low) if imp_low else None,
            fields=parts,
            plaintext=plaintext,
        )
    return ScaleEvent(type="raw", fields=parts, plaintext=plaintext)


class CmtpAssembler:
    """Reassembles multi-frame CMTP messages.

    Header: 00 00 00 SS LL 00  -> reply RCV_RDY, expect LL data frames
    Data:   NN 00 <payload>    -> after the last one reply RCV_OK
    """

    def __init__(self) -> None:
        self.expected = 0
        self.buf = b""

    def feed(self, data: bytes) -> tuple[bytes | None, bytes | None]:
        """Return (ack_to_write, complete_ciphertext)."""
        if len(data) >= 6 and data[:3] == b"\x00\x00\x00" and data[5] == 0:
            self.expected = data[4]
            self.buf = b""
            return RCV_RDY, None
        if len(data) >= 2 and data[1] == 0 and self.expected > 0:
            self.buf += data[2:]
            if data[0] >= self.expected:
                full = self.buf
                self.expected = 0
                self.buf = b""
                return RCV_OK, full
        return None, None


class S400Session:
    """Logged-in session over an already connected Bleak-compatible client."""

    def __init__(self, client: Any, token: bytes) -> None:
        self.client = client
        self.token = token
        self.hub = NotifyHub()
        self.keys: SessionKeys | None = None
        self._assembler = CmtpAssembler()
        self._pending: list[ScaleEvent] = []
        self.disconnected = False

    async def login(self) -> None:
        for uuid in NOTIFY_UUIDS:
            try:
                await self.client.start_notify(uuid, self.hub.make_callback(uuid))
            except Exception as err:  # noqa: BLE001 - optional characteristics
                _LOGGER.debug("subscribe %s failed (skipping): %s", uuid[4:8], err)
        await asyncio.sleep(0.3)
        self.keys = await login(self.client, self.token, self.hub)

    def mark_disconnected(self) -> None:
        self.disconnected = True
        self.hub.queue(CMTP).put_nowait(_DISCONNECTED)

    async def next_event(self, timeout: float) -> ScaleEvent | None:
        """Return the next decoded event, or None on timeout/disconnect."""
        assert self.keys is not None
        q = self.hub.queue(CMTP)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._pending:
            remaining = deadline - loop.time()
            if remaining <= 0 or self.disconnected:
                return None
            try:
                data = await asyncio.wait_for(q.get(), timeout=remaining)
            except TimeoutError:
                return None
            if data == _DISCONNECTED and self.disconnected:
                return None
            ack, full = self._assembler.feed(data)
            if ack is not None:
                await self.client.write_gatt_char(CMTP, ack, response=False)
            if full is None:
                continue
            plaintext = decrypt_cmtp(self.keys, full)
            if plaintext is None:
                _LOGGER.warning("Failed to decrypt CMTP message %s", full.hex())
                continue
            _LOGGER.debug("plaintext %s", plaintext.hex(" "))
            self._pending.append(parse_payload(plaintext))
        return self._pending.pop(0)
