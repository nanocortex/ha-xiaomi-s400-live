"""Connection manager for the S400: waits for advertisements, connects, streams events."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    AFTER_FINAL_GRACE,
    CONF_BIRTHDAY,
    CONF_HEIGHT,
    CONF_PROFILE_ID,
    CONF_SEX,
    CONF_TOKEN,
    EVENT_MEASUREMENT,
    IDLE_TIMEOUT,
    RECONNECT_COOLDOWN,
    SUBENTRY_PERSON,
)
from .s400 import AuthError, BadTokenError, S400Session, ScaleEvent, UserProfile, compute

_LOGGER = logging.getLogger(__name__)


def age_from_birthday(birthday: str, today: date | None = None) -> int:
    """Return full years since birthday (YYYY-MM-DD)."""
    born = date.fromisoformat(birthday)
    today = today or dt_util.now().date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


@dataclass
class Person:
    """A person configured as a subentry."""

    subentry_id: str
    name: str
    profile_id: int
    sex: str
    birthday: str
    height: int

    @property
    def profile(self) -> UserProfile:
        return UserProfile(sex=self.sex, age_years=age_from_birthday(self.birthday), height_cm=self.height)


@dataclass
class Measurement:
    """A completed measurement."""

    profile_id: int | None
    weight_kg: float | None
    impedance_ohm: float | None
    impedance_low_ohm: float | None
    measured_at: datetime
    body: dict[str, Any] = field(default_factory=dict)
    raw_fields: list[str] = field(default_factory=list)


@dataclass
class ScaleState:
    """Shared, live state of the scale."""

    connected: bool = False
    on_scale: bool = False
    live_weight: float | None = None
    stable: bool = False
    last_profile_id: int | None = None
    last_measurement: Measurement | None = None
    last_error: str | None = None
    last_raw_fields: list[str] = field(default_factory=list)


class S400Coordinator:
    """Owns the BLE session lifecycle and fans out updates to entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self.name: str = entry.data.get(CONF_NAME) or entry.title
        self.token = bytes.fromhex(entry.data[CONF_TOKEN])
        self.state = ScaleState()
        self.persons: dict[int, Person] = {}
        self.person_measurements: dict[str, Measurement] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsub: list[CALLBACK_TYPE] = []
        self._task: asyncio.Task | None = None
        self._last_session_end = 0.0
        self._stopping = False
        self._load_persons()

    def _load_persons(self) -> None:
        self.persons = {}
        for sub in self.entry.subentries.values():
            if sub.subentry_type != SUBENTRY_PERSON:
                continue
            d = sub.data
            self.persons[int(d[CONF_PROFILE_ID])] = Person(
                subentry_id=sub.subentry_id,
                name=sub.title,
                profile_id=int(d[CONF_PROFILE_ID]),
                sex=d[CONF_SEX],
                birthday=d[CONF_BIRTHDAY],
                height=int(d[CONF_HEIGHT]),
            )

    # ---- listeners -------------------------------------------------------

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # ---- lifecycle -------------------------------------------------------

    async def async_start(self) -> None:
        self._unsub.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                {"address": self.address, "connectable": True},
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )

    async def async_stop(self) -> None:
        self._stopping = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @callback
    def _async_on_advertisement(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        if self._stopping or (self._task and not self._task.done()):
            return
        if time.monotonic() - self._last_session_end < RECONNECT_COOLDOWN:
            return
        _LOGGER.debug("Advertisement from %s (rssi %s), connecting", self.address, service_info.rssi)
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_session(service_info.device), f"{self.address} S400 session"
        )

    # ---- session ---------------------------------------------------------

    async def _async_session(self, device: Any) -> None:
        client = None
        try:
            await close_stale_connections_by_address(self.address)
            session_ref: list[S400Session] = []

            def _disconnected(_client: Any) -> None:
                _LOGGER.debug("%s disconnected", self.address)
                if session_ref:
                    session_ref[0].mark_disconnected()

            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                disconnected_callback=_disconnected,
                ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                )
                or device,
            )
            session = S400Session(client, self.token)
            session_ref.append(session)
            self.state.connected = True
            self.state.last_error = None
            self._notify()

            await session.login()
            _LOGGER.debug("Logged in to %s", self.address)
            await self._async_pump(session)
        except BadTokenError:
            _LOGGER.error("S400 %s rejected the login token. Re-extract it and reconfigure.", self.address)
            self.state.last_error = "bad_token"
            self.entry.async_start_reauth(self.hass)
        except (AuthError, BleakError, TimeoutError) as err:
            _LOGGER.debug("S400 session with %s failed: %s", self.address, err)
            self.state.last_error = str(err) or type(err).__name__
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._last_session_end = time.monotonic()
            self.state.connected = False
            self.state.on_scale = False
            self._notify()

    async def _async_pump(self, session: S400Session) -> None:
        got_final = False
        while not self._stopping:
            event = await session.next_event(AFTER_FINAL_GRACE if got_final else IDLE_TIMEOUT)
            if event is None:
                return
            if event.type == "live":
                self.state.on_scale = True
                self.state.live_weight = event.weight_kg
                self.state.stable = event.stable
                self._notify()
            elif event.type == "final":
                got_final = True
                self._handle_final(event)
            else:
                _LOGGER.debug("Unparsed frame %s fields=%s", event.plaintext.hex(" "), event.fields)

    @callback
    def _handle_final(self, event: ScaleEvent) -> None:
        _LOGGER.debug("Final frame fields: %s", event.fields)
        if event.weight_kg is None:
            return
        measured_at = dt_util.utcnow()
        if event.timestamp and event.timestamp > 1_600_000_000:
            measured_at = dt_util.utc_from_timestamp(event.timestamp)

        measurement = Measurement(
            profile_id=event.profile_id,
            weight_kg=event.weight_kg,
            impedance_ohm=event.impedance_ohm,
            impedance_low_ohm=event.impedance_low_ohm,
            measured_at=measured_at,
            raw_fields=event.fields,
        )
        person = self.persons.get(event.profile_id) if event.profile_id is not None else None
        impedance = event.impedance_ohm or event.impedance_low_ohm
        if person and impedance:
            try:
                measurement.body = compute(event.weight_kg, impedance, person.profile)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Body composition failed: %s", err)
        elif person:
            measurement.body = {"bmi": round(event.weight_kg / (person.height / 100) ** 2, 1)}

        self.state.live_weight = event.weight_kg
        self.state.stable = True
        self.state.last_profile_id = event.profile_id
        self.state.last_measurement = measurement
        self.state.last_raw_fields = event.fields
        if person:
            self.person_measurements[person.subentry_id] = measurement
        self._notify()

        self.hass.bus.async_fire(
            EVENT_MEASUREMENT,
            {
                "address": self.address,
                "profile_id": event.profile_id,
                "person": person.name if person else None,
                "weight": event.weight_kg,
                "impedance": event.impedance_ohm,
                "impedance_low": event.impedance_low_ohm,
                "measured_at": measured_at.isoformat(),
                **measurement.body,
            },
        )
