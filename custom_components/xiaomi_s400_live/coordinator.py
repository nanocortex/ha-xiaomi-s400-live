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
    CONF_BINDKEY,
    CONF_BIRTHDAY,
    CONF_HEIGHT,
    CONF_PROFILE_ID,
    CONF_SEX,
    CONF_TOKEN,
    EVENT_MEASUREMENT,
    HEART_RATE_WAIT,
    IDLE_TIMEOUT,
    MAX_RECONNECT_BACKOFF,
    MIBEACON_UUID,
    RECONNECT_COOLDOWN,
    SUBENTRY_PERSON,
)
from .s400 import AuthError, BadTokenError, S400Session, ScaleEvent, UserProfile, compute
from .s400.mibeacon import decode_advertisement

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
    heart_rate: int | None = None
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
    heart_rate: int | None = None
    last_raw_fields: list[str] = field(default_factory=list)


class S400Coordinator:
    """Owns the BLE session lifecycle and fans out updates to entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self.name: str = entry.data.get(CONF_NAME) or entry.title
        self.token = bytes.fromhex(entry.data[CONF_TOKEN])
        bindkey = entry.data.get(CONF_BINDKEY)
        self.bindkey = bytes.fromhex(bindkey) if bindkey else None
        self._hr_deadline = 0.0
        self._pending_event: tuple[Measurement, Person | None] | None = None
        self._pending_handle: asyncio.TimerHandle | None = None
        self._last_adv_hr: tuple[float, int, int] | None = None  # (monotonic, profile_id, bpm)
        self.state = ScaleState()
        self.persons: dict[int, Person] = {}
        self.person_measurements: dict[str, Measurement] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsub: list[CALLBACK_TYPE] = []
        self._task: asyncio.Task | None = None
        self._last_session_end = 0.0
        self._backoff = RECONNECT_COOLDOWN
        self._last_counter: int | None = None
        self._session_had_data = False
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
        if self._pending_handle:
            self._pending_handle.cancel()
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
        data = service_info.service_data.get(MIBEACON_UUID) or b""
        counter = data[4] if len(data) >= 5 else None
        frctrl = data[0] | (data[1] << 8) if len(data) >= 2 else 0
        if counter is not None and counter != self._last_counter:
            _LOGGER.debug(
                "Adv %s: counter %s -> %s, object=%s, len=%s, rssi=%s",
                self.address, self._last_counter, counter, (frctrl >> 6) & 1, len(data), service_info.rssi,
            )
            if self._last_counter is not None:
                self._backoff = RECONNECT_COOLDOWN  # scale has something new: retry soon
            self._last_counter = counter
        if self.bindkey:
            self._check_heart_rate(service_info)
        if self._stopping or (self._task and not self._task.done()):
            return
        if time.monotonic() - self._last_session_end < self._backoff:
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
            self.state.stable = False
            self.state.last_error = None
            self._notify()

            await session.login()
            _LOGGER.debug("Logged in to %s, waiting for frames", self.address)
            self._session_had_data = False
            await self._async_pump(session)
            _LOGGER.debug("Session with %s ended (data received: %s)", self.address, self._session_had_data)
        except BadTokenError:
            _LOGGER.error("S400 %s rejected the login token. Re-extract it and reconfigure.", self.address)
            self.state.last_error = "bad_token"
            self.entry.async_start_reauth(self.hass)
        except (AuthError, BleakError, TimeoutError) as err:
            _LOGGER.debug("S400 session with %s failed: %s", self.address, err)
            self.state.last_error = str(err) or type(err).__name__
        except Exception as err:
            _LOGGER.exception("Unexpected error in S400 session with %s", self.address)
            self.state.last_error = str(err) or type(err).__name__
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._last_session_end = time.monotonic()
            if self._session_had_data:
                self._backoff = RECONNECT_COOLDOWN
            else:
                # Nothing happened: back off so we don't keep a proxy slot busy for nothing.
                self._backoff = min(self._backoff * 2, MAX_RECONNECT_BACKOFF)
            _LOGGER.debug("Next connection attempt in >= %.0f s", self._backoff)
            self.state.connected = False
            self.state.on_scale = False
            self.state.stable = False
            self._notify()

    async def _async_pump(self, session: S400Session) -> None:
        got_final = False
        while not self._stopping:
            event = await session.next_event(AFTER_FINAL_GRACE if got_final else IDLE_TIMEOUT)
            if event is None:
                return
            self._session_had_data = True
            _LOGGER.debug("Event %s fields=%s", event.type, event.fields)
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
        self._hr_deadline = time.monotonic() + HEART_RATE_WAIT
        # The heart-rate advertisement may already have arrived before the GATT final frame.
        if self._last_adv_hr and time.monotonic() - self._last_adv_hr[0] < HEART_RATE_WAIT:
            if self._last_adv_hr[1] == event.profile_id:
                measurement.heart_rate = self._last_adv_hr[2]
        self._notify()

        self._fire_pending()  # flush a previous measurement still waiting for heart rate
        if self.bindkey and measurement.heart_rate is None:
            # Heart rate arrives in a later advertisement; fire the event once we have it (or give up).
            self._pending_event = (measurement, person)
            self._pending_handle = self.hass.loop.call_later(HEART_RATE_WAIT, self._fire_pending)
        else:
            self._fire(measurement, person)

    @callback
    def _check_heart_rate(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        data = service_info.service_data.get(MIBEACON_UUID)
        if not data or self.bindkey is None:
            return
        reading = decode_advertisement(data, self.address, self.bindkey)
        if reading is None:
            return
        _LOGGER.debug("Advertisement reading: %s", reading)
        if reading.heart_rate is not None:
            self.state.heart_rate = reading.heart_rate
            self._last_adv_hr = (time.monotonic(), reading.profile_id, reading.heart_rate)
        m = self.state.last_measurement
        if (
            reading.heart_rate is None
            or m is None
            or m.heart_rate is not None
            or time.monotonic() > self._hr_deadline
            or reading.profile_id != m.profile_id
        ):
            self._notify()
            return
        m.heart_rate = reading.heart_rate
        self._notify()
        self._fire_pending()

    @callback
    def _fire_pending(self) -> None:
        pending = self._pending_event
        if self._pending_handle:
            self._pending_handle.cancel()
        self._pending_handle = None
        if pending is None:
            return
        self._pending_event = None
        self._fire(*pending)

    @callback
    def _fire(self, measurement: Measurement, person: Person | None) -> None:
        self.hass.bus.async_fire(
            EVENT_MEASUREMENT,
            {
                "address": self.address,
                "profile_id": measurement.profile_id,
                "person": person.name if person else None,
                "weight": measurement.weight_kg,
                "impedance": measurement.impedance_ohm,
                "impedance_low": measurement.impedance_low_ohm,
                "heart_rate": measurement.heart_rate,
                "measured_at": measurement.measured_at.isoformat(),
                **measurement.body,
            },
        )
