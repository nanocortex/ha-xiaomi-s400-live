"""Home Assistant level tests: config flow, subentries, entities fed by the fake scale."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_s400_live.const import (
    CONF_BIRTHDAY,
    CONF_HEIGHT,
    CONF_PROFILE_ID,
    CONF_SEX,
    CONF_TOKEN,
    DOMAIN,
    EVENT_MEASUREMENT,
)

from .fake_scale import FakeScale

ADDRESS = "AA:BB:CC:DD:EE:FF"
TOKEN_HEX = "00112233445566778899aabb"
FINAL_ASIA = "0,0,2,612,1,2,1760607000," + ",".join(["0"] * 23) + ",5650,5120"
FINAL_DAVE = "0,0,1,909,1,2,1760607100," + ",".join(["0"] * 23) + ",4890,4413"


@pytest.fixture(autouse=True)
def no_bluetooth():
    """Stub HA bluetooth so tests don't need a real adapter."""
    with (
        patch("custom_components.xiaomi_s400_live.coordinator.bluetooth.async_register_callback", return_value=lambda: None),
        patch("custom_components.xiaomi_s400_live.config_flow.async_discovered_service_info", return_value=[]),
    ):
        yield


async def test_user_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: "bad"})
    assert result["errors"] == {CONF_ADDRESS: "invalid_mac"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS.lower()})
    assert result["step_id"] == "credentials"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "zz"})
    assert result["errors"] == {CONF_TOKEN: "invalid_token"}

    with patch("custom_components.xiaomi_s400_live.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: TOKEN_HEX.upper(), CONF_NAME: "Waga"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_NAME: "Waga", CONF_TOKEN: TOKEN_HEX}


def _entry(persons: list[tuple[str, int, str]]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Waga",
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_NAME: "Waga", CONF_TOKEN: TOKEN_HEX},
        subentries_data=[
            ConfigSubentryData(
                subentry_type="person",
                title=name,
                unique_id=None,
                data={CONF_PROFILE_ID: pid, CONF_SEX: sex, CONF_BIRTHDAY: "1990-01-01", CONF_HEIGHT: 175},
            )
            for name, pid, sex in persons
        ],
    )


async def test_person_subentry_flow(hass: HomeAssistant) -> None:
    entry = _entry([("Dave", 1, "male")])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init((entry.entry_id, "person"), context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    data = {CONF_NAME: "Asia", CONF_PROFILE_ID: 1, CONF_SEX: "female", CONF_BIRTHDAY: "1992-05-05", CONF_HEIGHT: 165}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
    assert result["errors"] == {CONF_PROFILE_ID: "profile_in_use"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {**data, CONF_PROFILE_ID: 2})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 2


async def test_measurement_routed_to_person(hass: HomeAssistant) -> None:
    entry = _entry([("Dave", 1, "male"), ("Asia", 2, "female")])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    events = []
    hass.bus.async_listen(EVENT_MEASUREMENT, events.append)

    async def run(final_csv: str, live: list[str]) -> None:
        scale = FakeScale(bytes.fromhex(TOKEN_HEX))

        async def fake_connect(*args, **kwargs):
            return scale

        scale.disconnect = AsyncMock()
        with (
            patch("custom_components.xiaomi_s400_live.coordinator.establish_connection", side_effect=fake_connect),
            patch("custom_components.xiaomi_s400_live.coordinator.close_stale_connections_by_address", AsyncMock()),
            patch("custom_components.xiaomi_s400_live.coordinator.AFTER_FINAL_GRACE", 0.05),
        ):
            task = hass.async_create_task(coordinator._async_session(object()))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if scale.state == "ready":
                    break
            for w in live:
                scale.send_csv(w)
            scale.send_csv(final_csv)
            await asyncio.wait_for(task, 5)
        await hass.async_block_till_done()

    await run(FINAL_ASIA, ["400,0", "612,1"])

    reg = er.async_get(hass)
    ids = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    dave_sub = next(s for s in entry.subentries.values() if s.title == "Dave").subentry_id
    asia_sub = next(s for s in entry.subentries.values() if s.title == "Asia").subentry_id

    def state(uid: str) -> str:
        return hass.states.get(ids[uid]).state

    assert state(f"{ADDRESS}_{asia_sub}_weight") == "61.2"
    assert state(f"{ADDRESS}_{dave_sub}_weight") == "unknown"
    assert state(f"{ADDRESS}_live_weight") == "61.2"
    assert state(f"{ADDRESS}_profile_id") == "2"
    assert float(state(f"{ADDRESS}_{asia_sub}_fat_percent")) > 0
    assert state(f"{ADDRESS}_connected") == "off"
    assert events[-1].data["person"] == "Asia"

    await run(FINAL_DAVE, ["909,1"])
    assert state(f"{ADDRESS}_{dave_sub}_weight") == "90.9"
    assert state(f"{ADDRESS}_{asia_sub}_weight") == "61.2"
    assert state(f"{ADDRESS}_{dave_sub}_impedance") == "489.0"

    assert await hass.config_entries.async_unload(entry.entry_id)
