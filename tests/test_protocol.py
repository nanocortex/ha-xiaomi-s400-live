"""Protocol-level tests: login handshake, CMTP reassembly, parsing, body composition."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.xiaomi_s400_live.config_flow import normalize_token
from custom_components.xiaomi_s400_live.coordinator import age_from_birthday
from custom_components.xiaomi_s400_live.s400 import (
    BadTokenError,
    S400Session,
    UserProfile,
    compute,
    parse_payload,
)

from .fake_scale import FakeScale, csv_plaintext

TOKEN = bytes.fromhex("00112233445566778899aabb")
FINAL_CSV = "0,0,2,905,1,2,1760607000," + ",".join(["0"] * 23) + ",4890,4413"


def test_parse_live() -> None:
    ev = parse_payload(csv_plaintext("907,0"))
    assert ev.type == "live"
    assert ev.weight_kg == pytest.approx(90.7)
    assert ev.stable is False


def test_parse_final() -> None:
    ev = parse_payload(csv_plaintext(FINAL_CSV))
    assert ev.type == "final"
    assert ev.profile_id == 2
    assert ev.weight_kg == pytest.approx(90.5)
    assert ev.timestamp == 1760607000
    assert ev.impedance_ohm == pytest.approx(489.0)
    assert ev.impedance_low_ohm == pytest.approx(441.3)
    assert len(ev.fields) == 32


def test_parse_unknown() -> None:
    assert parse_payload(b"\x01\x02\x03").type == "raw"


def test_body_composition() -> None:
    body = compute(90.5, 489.0, UserProfile(sex="male", age_years=35, height_cm=180))
    assert body["bmi"] == pytest.approx(27.9, abs=0.05)
    assert 5 < body["fat_percent"] < 45
    assert body["body_type_name"]


def test_age() -> None:
    from datetime import date

    assert age_from_birthday("1990-10-17", date(2026, 10, 16)) == 35
    assert age_from_birthday("1990-10-16", date(2026, 10, 16)) == 36


def test_normalize_token() -> None:
    assert normalize_token(" 00 11 22 33 44 55 66 77 88 99 AA BB ") == "00112233445566778899aabb"
    with pytest.raises(ValueError):
        normalize_token("abc")


async def test_login_and_stream() -> None:
    scale = FakeScale(TOKEN)
    session = S400Session(scale, TOKEN)
    await session.login()
    assert scale.login_ok

    scale.send_csv("812,0")
    scale.send_csv("905,1")
    scale.send_csv(FINAL_CSV)
    events = [await session.next_event(2) for _ in range(3)]
    assert [e.type for e in events] == ["live", "live", "final"]
    assert events[1].stable
    assert events[2].profile_id == 2
    assert await session.next_event(0.1) is None


async def test_login_bad_token() -> None:
    scale = FakeScale(TOKEN)
    session = S400Session(scale, bytes(12))
    with pytest.raises(BadTokenError):
        await session.login()


async def test_disconnect_ends_stream() -> None:
    scale = FakeScale(TOKEN)
    session = S400Session(scale, TOKEN)
    await session.login()
    asyncio.get_running_loop().call_later(0.05, session.mark_disconnected)
    assert await session.next_event(5) is None
