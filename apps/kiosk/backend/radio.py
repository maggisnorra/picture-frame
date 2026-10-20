"""Weekly radio policy and a player whose gain never changes the speaker volume."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator
from brightness import Location

UTC = timezone.utc
FADE_SECONDS = 120
MANUAL_FADE_SECONDS = 5
MANUAL_DURATION = timedelta(hours=8)
DEFAULT_STATION_URL = "https://ruv-radio-live.akamaized.net/streymi/ras2/ras2.m3u8"
logger = logging.getLogger(__name__)


class RadioSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=80)
    start_date: date
    weekday: int = Field(strict=True, ge=0, le=6)  # Monday = 0
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(strict=True, ge=1, le=1440)
    enabled: bool = True


class RadioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    station_url: HttpUrl = Field(default=DEFAULT_STATION_URL, validate_default=True)
    schedules: list[RadioSchedule] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_settings(self):
        if len({entry.id for entry in self.schedules}) != len(self.schedules):
            raise ValueError("Schedule IDs must be unique")
        return self


class RadioPlayback(BaseModel):
    """Persist manual intent so restarting cannot undo Stop or reset its timer."""

    model_config = ConfigDict(extra="forbid")
    manual_started_at: AwareDatetime | None = None
    fade_in_started_at: AwareDatetime | None = None
    play_factor: float = Field(default=0, ge=0, le=1)
    stopped_at: AwareDatetime | None = None
    stop_factor: float = Field(default=0, ge=0, le=1)


@dataclass
class RadioPolicy:
    factor: float
    active: bool
    schedule_ids: list[str]
    mode: str
    manual_until: datetime | None = None


def active_radio(settings: RadioSettings, now: datetime, timezone_name: str | None, *,
                 starts_after: datetime | None = None) -> tuple[float, list[str]]:
    """Recover active windows after restart, including windows crossing midnight.

    Durations are real elapsed minutes. Skip nonexistent DST times and use the first
    occurrence of ambiguous times. Overlaps share one stream, using the larger gain.
    """
    if timezone_name is None:
        return 0.0, []
    now = now.astimezone(UTC)
    tz = ZoneInfo(timezone_name)
    today = now.astimezone(tz).date()
    factor = 0.0
    active = []
    for entry in settings.schedules:
        if not entry.enabled:
            continue
        for offset in (0, 1, 2):
            day = today - timedelta(days=offset)
            if day < entry.start_date or day.weekday() != entry.weekday:
                continue
            local = datetime.combine(day, clock_time.fromisoformat(entry.start_time), tzinfo=tz)
            start = local.astimezone(UTC)
            if starts_after is not None and start <= starts_after:
                continue
            if start.astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
                continue
            elapsed = (now - start).total_seconds()
            remaining = entry.duration_minutes * 60 - elapsed
            if elapsed >= 0 and remaining > 0:
                active.append(entry.id)
                # An occurrence starting during a manual stop waits for that
                # fade to reach zero, then starts its own fade from zero too.
                fade_start = max(start, starts_after + timedelta(seconds=MANUAL_FADE_SECONDS)) if starts_after else start
                fade_elapsed = max(0.0, (now - fade_start).total_seconds())
                factor = max(factor, min(1.0, fade_elapsed / FADE_SECONDS, remaining / FADE_SECONDS))
    return factor, active


class MpvPlayer:
    """mpv's software volume is the radio-only factor; PulseAudio owns base volume."""

    def __init__(self, sink: str):
        self.sink = sink
        self.process: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self._directory: tempfile.TemporaryDirectory | None = None
        self._socket: str | None = None

    @property
    def playing(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def command(self, *command):
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self._socket), 1)
        try:
            writer.write((json.dumps({"command": command, "request_id": 1}) + "\n").encode())
            await writer.drain()
            async with asyncio.timeout(1):
                while line := await reader.readline():
                    reply = json.loads(line)
                    if reply.get("request_id") == 1:
                        if reply.get("error") != "success":
                            raise RuntimeError(f"Radio player: {reply.get('error')}")
                        return reply.get("data")
            raise RuntimeError("Radio player disconnected")
        finally:
            writer.close()
            await writer.wait_closed()

    async def apply(self, url: str, factor: float):
        if not self.playing or self.url != url:
            await self.stop()
            self._directory = tempfile.TemporaryDirectory(prefix="frame-radio-")
            self._socket = str(Path(self._directory.name) / "mpv.sock")
            try:
                self.process = await asyncio.create_subprocess_exec(
                    "mpv", "--no-config", "--load-scripts=no", "--ytdl=no", "--no-video",
                    "--no-terminal", "--volume=0", "--volume-max=100", "--replaygain=no",
                    f"--audio-device=pulse/{self.sink}", f"--input-ipc-server={self._socket}",
                    "--network-timeout=10", "--", url,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self.url = url
                for _ in range(30):
                    if not self.playing:
                        raise RuntimeError("Radio player exited; check the stream URL and audio device")
                    if Path(self._socket).exists():
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Radio player did not start")
            except BaseException:
                await self.stop()
                raise
        # mpv cubes its volume percentage. Undo that curve so the actual signal
        # multiplier is exactly our linear 0..1 fade factor.
        percent = max(0.0, min(1.0, factor)) ** (1 / 3) * 100
        await self.command("set_property", "volume", round(percent, 6))

    async def stop(self):
        if self.process is not None:
            if self.playing:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        self.url = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


class RadioController:
    def __init__(self, path: Path, player: MpvPlayer, *,
                 location_provider: Callable[[], tuple[Location | None, str | None]] = lambda: (None, None)):
        self.path = path
        self.player = player
        self.location_provider = location_provider
        self.settings = RadioSettings()
        self.playback = RadioPlayback()
        self._factor = 0.0
        self._poll_interval = 0.5
        self.error: str | None = None
        self.storage_error: str | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._retry_at = 0.0
        self._lock = asyncio.Lock()
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    saved.pop("timezone", None)  # Migrate schedules saved before timezone followed location.
                    playback = RadioPlayback.model_validate(saved.pop("playback", {}))
                else:
                    playback = RadioPlayback()
                self.settings = RadioSettings.model_validate(saved)
                self.playback = playback
            except (ValueError, OSError) as exc:
                self.storage_error = f"Cannot load radio settings: {exc}"
                logger.exception("Cannot load radio settings")

    def _policy(self, now: datetime, timezone_name: str | None) -> RadioPolicy:
        stopped_at = self.playback.stopped_at
        stop_factor = self.playback.stop_factor
        if self.playback.manual_started_at is not None:
            until = self.playback.manual_started_at + MANUAL_DURATION
            if now < until:
                fade_start = self.playback.fade_in_started_at or self.playback.manual_started_at
                progress = max(0.0, min(1.0, (now - fade_start).total_seconds() / MANUAL_FADE_SECONDS))
                factor = self.playback.play_factor + (1 - self.playback.play_factor) * progress
                return RadioPolicy(factor, True, [], "manual", until)
            # Timeout behaves exactly like Stop, including cancelling occurrences
            # that began during the override. Derive it from the persisted deadline
            # so a restart after expiry cannot bring an old schedule back to life.
            stopped_at, stop_factor = until, 1.0
        if stopped_at is not None:
            elapsed = (now - stopped_at).total_seconds()
            if elapsed < MANUAL_FADE_SECONDS:
                factor = stop_factor * max(0.0, min(1.0, 1 - elapsed / MANUAL_FADE_SECONDS))
                return RadioPolicy(factor, factor > 0, [], "stopping")
        factor, active = active_radio(self.settings, now, timezone_name, starts_after=stopped_at)
        return RadioPolicy(factor, bool(active), active, "scheduled")

    def status(self, now: datetime | None = None):
        location, source = self.location_provider()
        timezone_name = location.timezone if location else None
        policy = self._policy(now or datetime.now(UTC), timezone_name)
        return {
            "settings": self.settings.model_dump(mode="json"),
            "timezone": timezone_name, "timezone_source": source,
            "timezone_error": None if location else "Radio scheduling is paused until the frame's location is available",
            "active": policy.active, "active_schedule_ids": policy.schedule_ids,
            "fade_factor": policy.factor, "fade_seconds": FADE_SECONDS,
            "mode": policy.mode,
            "manual_until": policy.manual_until.isoformat() if policy.manual_until else None,
            "manual_fade_seconds": MANUAL_FADE_SECONDS,
            "playing": self.player.playing,
            "error": self.error, "storage_error": self.storage_error,
        }

    def _persist(self, settings: RadioSettings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False, encoding="utf-8") as file:
                name = file.name
                saved = settings.model_dump(mode="json")
                saved["playback"] = self.playback.model_dump(mode="json")
                file.write(json.dumps(saved, indent=2))
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    async def update(self, settings: RadioSettings):
        async with self._lock:
            try:
                self._persist(settings)
            except OSError as exc:
                self.storage_error = f"Cannot save radio settings: {exc}"
                raise
            self.settings = settings.model_copy(deep=True)
            self.storage_error = None
            self._retry_at = 0
            self._wake.set()
        return self.status()

    async def _set_playback(self, playback: RadioPlayback, now: datetime):
        previous = self.playback
        self.playback = playback
        try:
            self._persist(self.settings)
        except OSError as exc:
            self.playback = previous
            self.storage_error = f"Cannot save radio playback: {exc}"
            raise
        self.storage_error = None
        self._retry_at = 0
        self._wake.set()
        await self._step_locked(now)
        return self.status(now)

    async def play(self, now: datetime | None = None):
        async with self._lock:
            now = now or datetime.now(UTC)
            factor = self._factor if self.player.playing else 0.0
            playback = RadioPlayback(manual_started_at=now, fade_in_started_at=now, play_factor=factor)
            if self.player.playing and self.playback.manual_started_at is not None:
                until = self.playback.manual_started_at + MANUAL_DURATION
                if now < until:
                    # Repeated Play refreshes only the override deadline. Keep an
                    # existing upward fade's trajectory and completion time.
                    playback.fade_in_started_at = self.playback.fade_in_started_at or self.playback.manual_started_at
                    playback.play_factor = self.playback.play_factor
            return await self._set_playback(playback, now)

    async def stop_playback(self, now: datetime | None = None):
        async with self._lock:
            now = now or datetime.now(UTC)
            factor = self._factor if self.player.playing else 0.0
            return await self._set_playback(RadioPlayback(stopped_at=now, stop_factor=factor), now)

    async def step(self, now: datetime | None = None):
        async with self._lock:
            await self._step_locked(now or datetime.now(UTC))

    async def _step_locked(self, now: datetime):
        location, _ = self.location_provider()
        policy = self._policy(now, location.timezone if location else None)
        self._poll_interval = 0.1 if policy.mode == "stopping" or (policy.mode == "manual" and policy.factor < 1) else 0.5
        if not policy.active:
            await self.player.stop()
            self._factor = 0.0
            self.error = None
            self._retry_at = 0
        elif time.monotonic() >= self._retry_at:
            try:
                await self.player.apply(str(self.settings.station_url), policy.factor)
                self._factor = policy.factor
                self.error = None
            except Exception as exc:
                await self.player.stop()
                self._factor = 0.0
                self.error = f"Cannot play radio: {exc}"
                self._retry_at = time.monotonic() + 15
                logger.warning("%s", self.error)

    async def start(self):
        async def worker():
            while True:
                self._wake.clear()
                try:
                    await self.step()
                except Exception:
                    logger.exception("Radio schedule step failed")
                try:
                    await asyncio.wait_for(self._wake.wait(), self._poll_interval)
                except asyncio.TimeoutError:
                    pass
        self._task = asyncio.create_task(worker())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.player.stop()


def radio_from_env(sink: str, *, location_provider: Callable[[], tuple[Location | None, str | None]]) -> RadioController:
    path = Path(os.environ.get("RADIO_STATE_PATH", str(Path(__file__).parent / "state/radio.json")))
    return RadioController(path, MpvPlayer(sink), location_provider=location_provider)
