"""Persisted software dimming policy; the kiosk browser renders the overlay."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable, Literal
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
    level_percent: Percent = 100
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
    def __init__(self, state_path: Path, *,
                 location_override: Location | None = None, location_fallback: Location | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 locate=lookup_location, on_change: Callable[[dict], None] | None = None):
        self.state_path = state_path
        self.location_override, self.location_fallback = location_override, location_fallback
        self.clock, self.locate, self.on_change = clock, locate, on_change
        self.state = BrightnessState()
        self.storage_error: str | None = None
        self._load_failed = False
        self.location_error: str | None = None
        self.schedule_error: str | None = None
        self.next_transition: datetime | None = None
        self._transition_seconds = 0.0
        self._next_lookup: datetime | None = None
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        try:
            if state_path.exists():
                self.state = BrightnessState.model_validate_json(state_path.read_text())
        except (OSError, ValueError, KeyError) as exc:
            self._load_failed = True
            self.storage_error = f"Cannot load brightness state: {exc}"
            # Never replace an unreadable/corrupt saved configuration automatically.
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

    def location(self, state: BrightnessState | None = None) -> tuple[Location | None, str | None]:
        state = state if state is not None else self.state
        if self.location_override:
            return self.location_override, "config_override"
        if state.cached_location:
            return state.cached_location.location, "ip_cache"
        return self.location_fallback, "config_fallback" if self.location_fallback else None

    def display_snapshot(self, *, initial=False) -> dict:
        # A commanded content level, never a measurement of the screen or backlight.
        return {"level_percent": self.state.level_percent,
                "transition_seconds": 0 if initial else self._transition_seconds}

    def _apply(self, updated: BrightnessState, *, manual=False, require_persistence=False):
        """Commit one complete policy decision while holding the controller lock."""
        now = self.clock()
        if updated.override and updated.override.expires_at <= now:
            updated.override = None
        location, _ = self.location(updated)
        self.schedule_error = None
        self.next_transition = None
        daylight = None
        if location:
            try:
                daylight, self.next_transition = solar_state(location, now)
            except (ValueError, OverflowError) as exc:
                self.schedule_error = f"Cannot calculate sun schedule: {exc}"
        else:
            self.schedule_error = "No usable location; automatic brightness is paused"
        if updated.enabled:
            if updated.override:
                updated.level_percent = updated.override.percent
            elif daylight is not None:
                updated.level_percent = updated.day_percent if daylight else updated.night_percent
        changed = updated.level_percent != self.state.level_percent
        if updated != self.state or require_persistence:
            try:
                self._persist(updated)
            except OSError:
                if require_persistence:
                    raise  # Remote mutations must not claim that unsaved settings were accepted.
                # An expired override must not keep controlling the display after a disk error.
        self.state = updated
        if changed or manual:
            # Publish even a repeated manual level: it must stop an automatic fade
            # whose destination happens to equal the new manual setting.
            self._transition_seconds = 0.0 if manual else 30.0
            if self.on_change:
                self.on_change(self.display_snapshot())

    async def tick(self):
        async with self._lock:
            self._apply(self.state.model_copy(deep=True))

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
                self._apply(updated, require_persistence=True)
                self.location_error = None
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
            self._apply(updated, manual=body.action == "manual", require_persistence=True)
            return self._status()

    def _status(self) -> dict:
        location, source = self.location()
        override = self.state.override
        if override and override.expires_at <= self.clock():
            override = None
        return {
            **self.display_snapshot(), "method": "software",
            "enabled": self.state.enabled,
            "mode": "disabled" if not self.state.enabled else "override" if override else "automatic",
            "day_percent": self.state.day_percent, "night_percent": self.state.night_percent,
            "override": override.model_dump(mode="json") if override else None,
            "next_transition": self.next_transition.isoformat() if self.next_transition else None,
            "location": location.model_dump() if location else None,
            "location_source": source,
            "errors": {"schedule": self.schedule_error, "location": self.location_error, "storage": self.storage_error},
        }

    async def status(self) -> dict:
        async with self._lock:
            return self._status()

    async def start(self):
        await self.tick()

        async def worker():
            while True:
                delay = 60.0
                if self.state.override:
                    delay = min(delay, max(0.1, (self.state.override.expires_at - self.clock()).total_seconds()))
                await asyncio.sleep(delay)
                try:
                    await self.tick()
                except Exception:
                    logger.exception("Brightness schedule step failed")

        async def locations():
            while True:
                await self.refresh_location()
                await asyncio.sleep(60)

        self._tasks = [asyncio.create_task(worker()), asyncio.create_task(locations())]

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []


def controller_from_env(*, on_change: Callable[[dict], None] | None = None) -> BrightnessController:
    def configured_location(name):
        raw = os.getenv(name)
        return Location.model_validate_json(raw) if raw else None

    return BrightnessController(
        Path(os.getenv("BRIGHTNESS_STATE_PATH", str(Path(__file__).parent / "state" / "brightness.json"))),
        location_override=configured_location("BRIGHTNESS_LOCATION_OVERRIDE"),
        location_fallback=configured_location("BRIGHTNESS_LOCATION_FALLBACK"),
        on_change=on_change,
    )
