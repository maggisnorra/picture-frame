"""Local brightness policy. No camera capture or display writes occur on import."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from astral import Observer
from astral.sun import elevation, sunrise, sunset
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

UTC = timezone.utc
Percent = Annotated[int, Field(strict=True, ge=5, le=100)]
logger = logging.getLogger(__name__)


class Location(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    timezone: str

    @model_validator(mode="after")
    def valid_timezone(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid IANA timezone: {self.timezone}") from exc
        return self


class CachedLocation(BaseModel):
    location: Location
    fetched_at: AwareDatetime


class Override(BaseModel):
    percent: Percent
    expires_at: AwareDatetime


class BrightnessState(BaseModel):
    enabled: bool = True
    day_percent: Percent = 80
    night_percent: Percent = 15
    override: Override | None = None
    cached_location: CachedLocation | None = None


class BrightnessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["settings", "manual", "resume"] = "settings"
    enabled: bool | None = None
    day_percent: Percent | None = None
    night_percent: Percent | None = None
    manual_percent: Percent | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if (self.action == "manual") != (self.manual_percent is not None):
            raise ValueError("manual_percent is required only for action=manual")
        if self.action != "settings" and self.enabled is not None:
            raise ValueError("enabled is only accepted for action=settings")
        return self


class DriverError(RuntimeError):
    pass


class Reading(BaseModel):
    percent: float
    requested_percent: float
    source: str
    device: str
    simulated: bool = False


class BrightnessDriver(Protocol):
    name: str

    def read(self) -> Reading: ...
    def write(self, percent: float) -> None: ...


class LinuxBacklight:
    name = "linux-backlight"

    def __init__(self, device: Path | None = None, root: Path = Path("/sys/class/backlight")):
        self.device = device
        self.root = root

    def _resolve(self) -> tuple[Path, int]:
        try:
            if self.device:
                devices = [self.device]
            else:
                devices = sorted(self.root.iterdir()) if self.root.is_dir() else []
            if not devices:
                raise DriverError("No Linux backlight found; run brightness_diagnostics.py")
            if len(devices) != 1:
                raise DriverError("Multiple backlights found; set BRIGHTNESS_DEVICE to one device directory")
            device = devices[0]
            maximum = int((device / "max_brightness").read_text().strip())
            if maximum <= 0:
                raise ValueError("max_brightness must be positive")
            return device, maximum
        except (OSError, ValueError) as exc:
            raise DriverError(f"Cannot inspect backlight: {exc}") from exc

    def read(self) -> Reading:
        device, maximum = self._resolve()
        try:
            requested = int((device / "brightness").read_text().strip())
            source = "actual_brightness" if (device / "actual_brightness").exists() else "brightness"
            actual = int((device / source).read_text().strip())
            if not 0 <= requested <= maximum or not 0 <= actual <= maximum:
                raise ValueError("backlight reading outside native range")
            return Reading(percent=100 * actual / maximum, requested_percent=100 * requested / maximum,
                           source=source, device=str(device))
        except (OSError, ValueError) as exc:
            raise DriverError(f"Cannot read backlight {device}: {exc}") from exc

    def write(self, percent: float) -> None:
        device, maximum = self._resolve()
        # Keep at least one native step lit, even on low-resolution devices.
        value = max(1, min(maximum, round(max(5, min(100, percent)) * maximum / 100)))
        try:
            # O_TRUNC matches shell redirection on sysfs, but omit O_CREAT.
            with os.fdopen(os.open(device / "brightness", os.O_WRONLY | os.O_TRUNC), "w") as output:
                output.write(str(value) + "\n")
        except OSError as exc:
            raise DriverError(f"Cannot write backlight {device}: {exc}") from exc


class SimulatedBacklight:
    name = "simulated"

    def __init__(self, percent: float = 40):
        self.percent = percent

    def read(self) -> Reading:
        return Reading(percent=self.percent, requested_percent=self.percent, source="simulation",
                       device="memory", simulated=True)

    def write(self, percent: float) -> None:
        self.percent = max(5, min(100, percent))


def solar_state(location: Location, now: datetime) -> tuple[bool, datetime | None]:
    """Return current daylight and the next transition within 24 hours, including polar days."""
    observer = Observer(location.latitude, location.longitude)
    tz = ZoneInfo(location.timezone)
    local_date = now.astimezone(tz).date()
    events: list[tuple[datetime, bool]] = []
    for offset in (-1, 0, 1, 2):
        date = local_date + timedelta(days=offset)
        for calculate, daylight in ((sunrise, True), (sunset, False)):
            try:
                events.append((calculate(observer, date=date, tzinfo=tz).astimezone(UTC), daylight))
            except ValueError:  # Polar day/night: that event does not occur on this date.
                continue
    events.sort()
    previous = [event for event in events if event[0] <= now]
    # Astral sunrise/sunset use refraction and the apparent solar radius.
    daylight = previous[-1][1] if previous else elevation(observer, now, with_refraction=False) > -0.833
    future = [event[0] for event in events if now < event[0] <= now + timedelta(hours=24)]
    return daylight, future[0] if future else None


async def lookup_location() -> Location:
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        response = await client.get("https://ipapi.co/json/")
        response.raise_for_status()
        data = response.json()
        return Location(latitude=data["latitude"], longitude=data["longitude"], timezone=data["timezone"])


class BrightnessController:
    def __init__(self, driver: BrightnessDriver, state_path: Path, *,
                 location_override: Location | None = None, location_fallback: Location | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 monotonic: Callable[[], float] = time.monotonic,
                 locate=lookup_location, fade_seconds: float = 30):
        self.driver, self.state_path = driver, state_path
        self.location_override, self.location_fallback = location_override, location_fallback
        self.clock, self.monotonic, self.locate = clock, monotonic, locate
        self.fade_seconds = fade_seconds
        self.state = BrightnessState()
        self.storage_error: str | None = None
        self._load_failed = False
        self.location_error: str | None = None
        self.schedule_error: str | None = None
        self.driver_error: str | None = None
        self.write_error: str | None = None
        self.reading: Reading | None = None
        self.target: int | None = None
        self.next_transition: datetime | None = None
        self._fade: tuple[float, float] | None = None
        self._last_written: float | None = None
        self._retry_write_at = 0.0
        self._last_evaluation = -math.inf
        self._next_lookup: datetime | None = None
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        try:
            if state_path.exists():
                self.state = BrightnessState.model_validate_json(state_path.read_text())
        except (OSError, ValueError, KeyError) as exc:
            self._load_failed = True
            self.storage_error = f"Cannot load brightness state: {exc}"
            # Never apply defaults over an unreadable/corrupt saved configuration.
            self.state.enabled = False

    def _persist(self, state: BrightnessState):
        temporary = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.state_path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(state.model_dump_json(indent=2))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.state_path)
            self.storage_error = None
            self._load_failed = False
        except OSError as exc:
            self.storage_error = f"Cannot save brightness state: {exc}"
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def location(self) -> tuple[Location | None, str | None]:
        if self.location_override:
            return self.location_override, "config_override"
        if self.state.cached_location:
            return self.state.cached_location.location, "ip_cache"
        return self.location_fallback, "config_fallback" if self.location_fallback else None

    async def _read(self):
        try:
            self.reading = await asyncio.to_thread(self.driver.read)
            self.driver_error = None
        except DriverError as exc:
            self.reading = None
            self.driver_error = str(exc)

    async def _evaluate(self, *, immediate=False):
        now = self.clock()
        self._last_evaluation = self.monotonic()
        override = self.state.override
        if override and override.expires_at <= now:
            updated = self.state.model_copy(deep=True)
            updated.override = None
            try:
                self._persist(updated)
                self.state = updated
            except OSError:
                pass  # An expired override never controls the screen, even if disk is unwritable.
            override = None
        self.schedule_error = None
        location, _ = self.location()
        self.next_transition = None
        daylight = None
        if location:
            try:
                daylight, self.next_transition = solar_state(location, now)
            except (ValueError, OverflowError) as exc:
                self.schedule_error = f"Cannot calculate sun schedule: {exc}"
        else:
            self.schedule_error = "No usable location; automatic brightness is paused"

        target = None
        if self.state.enabled:
            if override:
                target = override.percent
            elif daylight is not None:
                target = self.state.day_percent if daylight else self.state.night_percent
        if target != self.target or immediate:
            self.target = target
            self._fade = None
            self._last_written = None
            self._retry_write_at = 0
            if target is not None:
                await self._read()
                # Manual adjustments apply on the next worker tick; automatic transitions fade.
                if not immediate and self.reading and self.fade_seconds > 0:
                    self._fade = (self.monotonic(), self.reading.requested_percent)

    async def tick(self):
        """One serialized scheduler/driver step; exposed for deterministic tests."""
        async with self._lock:
            override = self.state.override
            if (self.monotonic() - self._last_evaluation >= 60 or
                    override is not None and override.expires_at <= self.clock()):
                await self._evaluate()
            if self.target is None or self.monotonic() < self._retry_write_at:
                return
            value = float(self.target)
            if self._fade:
                start, initial = self._fade
                fraction = min(1, max(0, (self.monotonic() - start) / self.fade_seconds))
                value = round(initial + (self.target - initial) * fraction, 1)
                if fraction == 1:
                    self._fade = None
            if value == self._last_written:
                return
            try:
                await asyncio.to_thread(self.driver.write, value)
                self.write_error = None
                self._last_written = value
                await self._read()
            except DriverError as exc:
                self.write_error = str(exc)
                self._retry_write_at = self.monotonic() + 60

    async def refresh_location(self):
        if self.location_override or self._load_failed:
            return
        now = self.clock()
        cached = self.state.cached_location
        if self._next_lookup and now < self._next_lookup:
            return
        if cached and now < cached.fetched_at + timedelta(days=1):
            return
        try:
            location = await self.locate()  # Network work never holds the controller lock.
            async with self._lock:
                updated = self.state.model_copy(deep=True)
                updated.cached_location = CachedLocation(location=location, fetched_at=self.clock())
                self._persist(updated)
                self.state = updated
                self.location_error = None
                await self._evaluate()
        except Exception as exc:
            self.location_error = f"IP location lookup/cache failed: {exc}"
            self._next_lookup = self.clock() + timedelta(hours=1)

    async def update(self, body: BrightnessUpdate) -> dict:
        async with self._lock:
            updated = self.state.model_copy(deep=True)
            for field in ("enabled", "day_percent", "night_percent"):
                value = getattr(body, field)
                if value is not None:
                    setattr(updated, field, value)
            if body.action == "manual":
                expires = updated.override.expires_at if updated.override and updated.override.expires_at > self.clock() else None
                if expires is None:
                    location, _ = self.location()
                    _, expires = solar_state(location, self.clock()) if location else (False, None)
                updated.override = Override(percent=body.manual_percent,
                                            expires_at=expires or self.clock() + timedelta(hours=24))
                updated.enabled = True
            elif body.action == "resume" or not updated.enabled:
                updated.override = None
            if body.action == "resume":
                updated.enabled = True
            self._persist(updated)  # Commit before changing policy or writing hardware.
            self.state = updated
            await self._evaluate(immediate=body.action == "manual")
        await self.tick()
        return await self.status()

    async def status(self) -> dict:
        async with self._lock:
            await self._read()
            location, source = self.location()
            override = self.state.override
            if override and override.expires_at <= self.clock():
                override = None
            return {
                "enabled": self.state.enabled,
                "mode": "disabled" if not self.state.enabled else "override" if override else "automatic",
                "day_percent": self.state.day_percent, "night_percent": self.state.night_percent,
                "target_percent": self.target,
                "reported_percent": self.reading.percent if self.reading else None,
                "reading": self.reading.model_dump() if self.reading else None,
                "override": override.model_dump(mode="json") if override else None,
                "next_transition": self.next_transition.isoformat() if self.next_transition else None,
                "location": location.model_dump() if location else None,
                "location_source": source,
                "location_cached_at": self.state.cached_location.fetched_at.isoformat() if self.state.cached_location else None,
                "driver": self.driver.name, "available": self.reading is not None,
                "simulated": self.driver.name == "simulated",
                "errors": {"hardware": self.driver_error or self.write_error, "schedule": self.schedule_error,
                           "location": self.location_error, "storage": self.storage_error},
            }

    async def start(self):
        await self.tick()

        async def worker():
            while True:
                await asyncio.sleep(0.5)
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Brightness worker step failed")

        async def locations():
            while True:
                await self.refresh_location()
                await asyncio.sleep(60)

        self._tasks = [asyncio.create_task(worker()), asyncio.create_task(locations())]

    async def stop(self):
        # Finish any in-flight hardware operation before cancellation, including its to_thread call.
        async with self._lock:
            for task in self._tasks:
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []


def controller_from_env() -> BrightnessController:
    driver_name = os.getenv("BRIGHTNESS_DRIVER", "linux")
    if driver_name not in ("linux", "simulated"):
        raise ValueError("BRIGHTNESS_DRIVER must be linux or simulated")
    device = os.getenv("BRIGHTNESS_DEVICE")
    driver = SimulatedBacklight() if driver_name == "simulated" else LinuxBacklight(Path(device) if device else None)

    def configured_location(name):
        raw = os.getenv(name)
        return Location.model_validate_json(raw) if raw else None

    return BrightnessController(
        driver, Path(os.getenv("BRIGHTNESS_STATE_PATH", str(Path(__file__).parent / "state" / "brightness.json"))),
        location_override=configured_location("BRIGHTNESS_LOCATION_OVERRIDE"),
        location_fallback=configured_location("BRIGHTNESS_LOCATION_FALLBACK"),
    )
