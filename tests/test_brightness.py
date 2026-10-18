import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from brightness import (
    BrightnessController, BrightnessState, BrightnessUpdate, CachedLocation, DriverError,
    LinuxBacklight, Location, Override, SimulatedBacklight, controller_from_env, lookup_location, solar_state,
)
from brightness_diagnostics import diagnostics

UTC = timezone.utc
AMSTERDAM = Location(latitude=52.37, longitude=4.90, timezone="Europe/Amsterdam")
TROMSO = Location(latitude=69.65, longitude=18.96, timezone="Europe/Oslo")


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 10, 16, 12, tzinfo=UTC)
        self.elapsed = 0.0

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.elapsed += seconds


def make_controller(tmp_path, clock=None, driver=None, **kwargs):
    clock = clock or Clock()
    options = dict(clock=lambda: clock.now, monotonic=lambda: clock.elapsed, location_fallback=AMSTERDAM)
    options.update(kwargs)
    return BrightnessController(driver or SimulatedBacklight(), tmp_path / "brightness.json", **options), clock


def run(coro):
    return asyncio.run(coro)


def device(tmp_path, name="panel", maximum="255", brightness="128", actual=None):
    panel = tmp_path / name
    panel.mkdir()
    (panel / "max_brightness").write_text(maximum)
    (panel / "brightness").write_text(brightness)
    if actual is not None:
        (panel / "actual_brightness").write_text(actual)
    return panel


def test_native_mapping_and_actual_readback(tmp_path):
    panel = device(tmp_path, actual="64")
    driver = LinuxBacklight(root=tmp_path)
    assert driver.read().percent == pytest.approx(64 / 255 * 100)
    assert driver.read().requested_percent == pytest.approx(128 / 255 * 100)
    driver.write(40)
    assert (panel / "brightness").read_text().strip() == "102"
    assert driver.read().percent == pytest.approx(64 / 255 * 100)
    assert driver.read().requested_percent == 40
    assert driver.read().source == "actual_brightness"


def test_mapping_low_resolution_and_shorter_writes(tmp_path):
    panel = device(tmp_path, maximum="100000", brightness="100000")
    driver = LinuxBacklight(root=tmp_path)
    driver.write(5)
    assert driver.read().percent == 5
    (panel / "max_brightness").write_text("1")
    driver.write(5)
    assert driver.read().percent == 100  # Never round a small native range to off.
    driver.write(1000)
    assert (panel / "brightness").read_text().strip() == "1"


def test_discovery_and_explicit_path(tmp_path):
    with pytest.raises(DriverError, match="No Linux backlight"):
        LinuxBacklight(root=tmp_path).read()
    first = device(tmp_path)
    device(tmp_path, "second")
    with pytest.raises(DriverError, match="Multiple backlights"):
        LinuxBacklight(root=tmp_path).read()
    assert LinuxBacklight(first).read().device == str(first)


@pytest.mark.parametrize("maximum", ["0", "-2", "garbage"])
def test_invalid_native_ranges(tmp_path, maximum):
    device(tmp_path, maximum=maximum)
    with pytest.raises(DriverError):
        LinuxBacklight(root=tmp_path).read()


def test_invalid_readings_and_missing_files(tmp_path):
    panel = device(tmp_path, brightness="999")
    driver = LinuxBacklight(panel)
    with pytest.raises(DriverError, match="outside native range"):
        driver.read()
    (panel / "brightness").unlink()
    with pytest.raises(DriverError):
        driver.write(50)
    assert not (panel / "brightness").exists()


def test_permission_failures_are_reported(tmp_path):
    panel = device(tmp_path)
    with patch("brightness.os.open", side_effect=PermissionError("fixture write denied")):
        with pytest.raises(DriverError, match="fixture write denied"):
            LinuxBacklight(panel).write(40)
    with patch.object(Path, "read_text", side_effect=PermissionError("fixture read denied")):
        with pytest.raises(DriverError, match="fixture read denied"):
            LinuxBacklight(panel).read()


@pytest.mark.parametrize("date,daylight", [("2026-10-16T12:00:00+00:00", True), ("2026-10-16T23:00:00+00:00", False),
                                          ("2026-03-29T12:00:00+00:00", True), ("2026-10-25T12:00:00+00:00", True)])
def test_solar_schedule_and_dst(date, daylight):
    now = datetime.fromisoformat(date)
    actual, transition = solar_state(AMSTERDAM, now)
    assert actual is daylight
    assert now < transition <= now + timedelta(hours=24)
    before, _ = solar_state(AMSTERDAM, transition - timedelta(seconds=1))
    after, _ = solar_state(AMSTERDAM, transition + timedelta(seconds=1))
    assert before != after


@pytest.mark.parametrize("month,daylight", [(6, True), (12, False)])
def test_polar_days(month, daylight):
    assert solar_state(TROMSO, datetime(2026, month, 21, 12, tzinfo=UTC)) == (daylight, None)


@pytest.mark.parametrize("location", [
    {"latitude": 91, "longitude": 0, "timezone": "UTC"},
    {"latitude": float("nan"), "longitude": 0, "timezone": "UTC"},
    {"latitude": 0, "longitude": 0, "timezone": "invented/place"},
])
def test_location_validation(location):
    with pytest.raises(ValidationError):
        Location(**location)


@pytest.mark.parametrize("payload", [{"manual_percent": 50}, {"action": "manual"}, {"action": "manual", "manual_percent": 0},
                                     {"day_percent": 101}, {"night_percent": 1.5}, {"day_percent": True},
                                     {"action": "resume", "enabled": False}, {"device": "/etc/passwd"}])
def test_update_validation(payload):
    with pytest.raises(ValidationError):
        BrightnessUpdate(**payload)


def test_day_night_fade_and_manual_cancel(tmp_path):
    async def scenario():
        service, clock = make_controller(tmp_path)
        await service.tick()
        assert service.target == 80
        clock.advance(15)
        await service.tick()
        assert service.driver.percent == 60
        await service.update(BrightnessUpdate(action="manual", manual_percent=25))
        assert service.driver.percent == 25
        clock.advance(10)
        await service.tick()
        assert service.driver.percent == 25
        await service.update(BrightnessUpdate(action="resume"))
        clock.advance(30)
        await service.tick()
        assert service.driver.percent == 80
        clock.advance(12 * 3600)
        await service.tick()
        clock.advance(30)
        await service.tick()
        assert service.driver.percent == 15
    run(scenario())


def test_override_expiry_repeated_adjustments_and_restart(tmp_path):
    async def scenario():
        service, clock = make_controller(tmp_path, fade_seconds=0)
        await service.update(BrightnessUpdate(action="manual", manual_percent=40))
        expires = service.state.override.expires_at
        clock.advance(60)
        await service.update(BrightnessUpdate(action="manual", manual_percent=35))
        assert service.state.override.expires_at == expires
        restored, _ = make_controller(tmp_path, clock, fade_seconds=0)
        await restored.tick()
        assert restored.driver.percent == 35
        clock.advance((expires - clock.now).total_seconds() + 1)
        await restored.tick()
        assert restored.state.override is None
        assert restored.driver.percent == 15
        restarted, _ = make_controller(tmp_path, clock, fade_seconds=0)
        await restarted.tick()
        assert restarted.state.override is None
        assert restarted.driver.percent == 15
    run(scenario())


@pytest.mark.parametrize("location", [None, TROMSO])
def test_24_hour_override_without_transition(tmp_path, location):
    async def scenario():
        clock = Clock(datetime(2026, 6, 21, 12, tzinfo=UTC))
        service, _ = make_controller(tmp_path, clock, location_fallback=location)
        await service.update(BrightnessUpdate(action="manual", manual_percent=30))
        assert service.state.override.expires_at == clock.now + timedelta(hours=24)
    run(scenario())


def test_no_location_and_disabled_retain_brightness(tmp_path):
    async def scenario():
        service, clock = make_controller(tmp_path, location_fallback=None)
        await service.tick()
        assert service.driver.percent == 40
        assert service.target is None
        assert (await service.status())["errors"]["schedule"]
        await service.update(BrightnessUpdate(action="manual", manual_percent=25))
        await service.update(BrightnessUpdate(enabled=False))
        clock.advance(86400)
        await service.tick()
        assert service.driver.percent == 25
        assert (await service.status())["mode"] == "disabled"
        await service.update(BrightnessUpdate(action="resume"))
        assert service.target is None
    run(scenario())


def test_location_refresh_caching_fallback_and_override(tmp_path):
    async def scenario():
        calls = []

        async def locate():
            calls.append(True)
            return TROMSO

        service, clock = make_controller(tmp_path, locate=locate)
        assert service.location() == (AMSTERDAM, "config_fallback")
        await service.refresh_location()
        assert service.location() == (TROMSO, "ip_cache")
        await service.refresh_location()
        assert len(calls) == 1
        restored, _ = make_controller(tmp_path, clock, locate=locate)
        await restored.refresh_location()
        assert len(calls) == 1
        clock.advance(86401)
        await restored.refresh_location()
        assert len(calls) == 2
        configured, _ = make_controller(tmp_path, clock, location_override=AMSTERDAM, locate=locate)
        await configured.refresh_location()
        assert configured.location() == (AMSTERDAM, "config_override")
        assert len(calls) == 2
    run(scenario())


def test_failed_lookup_retains_cache_and_retries_hourly(tmp_path):
    async def scenario():
        calls = []

        async def locate():
            calls.append(True)
            raise httpx.ConnectError("offline")

        service, clock = make_controller(tmp_path, locate=locate)
        service.state.cached_location = CachedLocation(location=TROMSO, fetched_at=clock.now - timedelta(days=2))
        await service.refresh_location()
        assert service.location() == (TROMSO, "ip_cache")
        assert service.location_error
        await service.refresh_location()
        assert len(calls) == 1
        clock.advance(3600)
        await service.refresh_location()
        assert len(calls) == 2
    run(scenario())


def test_lookup_response_validation():
    async def scenario():
        for body in [{"error": True}, {"latitude": 100, "longitude": 0, "timezone": "UTC"}]:
            async def get(*args, **kwargs):
                return httpx.Response(200, json=body, request=httpx.Request("GET", "https://ipapi.co/json/"))
            with patch("brightness.httpx.AsyncClient.get", new=get):
                with pytest.raises((KeyError, ValidationError)):
                    await lookup_location()
    run(scenario())


def test_write_error_survives_successful_status_read(tmp_path):
    class BrokenWriter(SimulatedBacklight):
        def write(self, percent):
            raise DriverError("write denied")

    async def scenario():
        service, _ = make_controller(tmp_path, driver=BrokenWriter())
        result = await service.update(BrightnessUpdate(action="manual", manual_percent=80))
        assert result["reported_percent"] == 40
        assert result["target_percent"] == 80
        assert result["errors"]["hardware"] == "write denied"
        assert (await service.status())["errors"]["hardware"] == "write denied"
    run(scenario())


def test_absent_hardware_is_not_success(tmp_path):
    async def scenario():
        service, _ = make_controller(tmp_path, driver=LinuxBacklight(root=tmp_path / "missing"))
        result = await service.update(BrightnessUpdate(action="manual", manual_percent=80))
        assert result["available"] is False
        assert result["reported_percent"] is None
        assert "No Linux backlight" in result["errors"]["hardware"]
    run(scenario())


def test_storage_failure_does_not_change_policy_or_write(tmp_path):
    async def scenario():
        service, _ = make_controller(tmp_path)
        with patch("brightness.os.replace", side_effect=PermissionError("disk denied")):
            with pytest.raises(PermissionError):
                await service.update(BrightnessUpdate(action="manual", manual_percent=80))
        assert service.state.override is None
        assert service.driver.percent == 40
        assert service.storage_error
        assert not list(tmp_path.iterdir())
    run(scenario())


def test_expired_override_cannot_survive_disk_error(tmp_path):
    async def scenario():
        service, clock = make_controller(tmp_path, fade_seconds=0)
        service.state.override = Override(percent=35, expires_at=clock.now - timedelta(seconds=1))
        with patch.object(service, "_persist", side_effect=PermissionError("disk denied")):
            await service.tick()
        assert service.target == 80
        assert service.driver.percent == 80
    run(scenario())


def test_corrupt_state_disables_control(tmp_path):
    (tmp_path / "brightness.json").write_text("{bad json")
    async def scenario():
        service, _ = make_controller(tmp_path)
        await service.tick()
        assert service.driver.percent == 40
        assert service.state.enabled is False
        assert service.storage_error
        await service.refresh_location()
        assert (tmp_path / "brightness.json").read_text() == "{bad json"
        assert service.storage_error
    run(scenario())


def test_failed_write_recovers_on_retry(tmp_path):
    class RecoveringDriver(SimulatedBacklight):
        broken = True

        def write(self, percent):
            if self.broken:
                raise DriverError("temporary failure")
            super().write(percent)

    async def scenario():
        driver = RecoveringDriver()
        service, clock = make_controller(tmp_path, driver=driver, fade_seconds=0)
        await service.tick()
        assert (await service.status())["errors"]["hardware"]
        driver.broken = False
        clock.advance(60)
        await service.tick()
        result = await service.status()
        assert result["reported_percent"] == 80
        assert result["errors"]["hardware"] is None
    run(scenario())


def test_network_lookup_never_blocks_manual_control(tmp_path):
    async def scenario():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def locate():
            started.set()
            await finish.wait()
            return TROMSO

        service, _ = make_controller(tmp_path, locate=locate)
        lookup = asyncio.create_task(service.refresh_location())
        await started.wait()
        result = await asyncio.wait_for(service.update(BrightnessUpdate(action="manual", manual_percent=35)), timeout=1)
        assert result["reported_percent"] == 35
        finish.set()
        await lookup
        assert service.state.override.percent == 35
    run(scenario())


def test_latest_manual_update_wins_and_writes_never_overlap(tmp_path):
    class RecordingDriver(SimulatedBacklight):
        def __init__(self):
            super().__init__()
            self.active = False
            self.writes = []

        def write(self, percent):
            assert not self.active
            self.active = True
            self.writes.append(percent)
            super().write(percent)
            self.active = False

    async def scenario():
        driver = RecordingDriver()
        service, clock = make_controller(tmp_path, driver=driver)
        await service.tick()
        await asyncio.gather(*(service.update(BrightnessUpdate(action="manual", manual_percent=n)) for n in [60, 30, 75]))
        clock.advance(30)
        await service.tick()
        assert driver.percent == 75
        assert driver.writes[-1] == 75
    run(scenario())


def test_controller_tasks_stop_cleanly(tmp_path):
    async def scenario():
        service, _ = make_controller(tmp_path, location_override=AMSTERDAM)
        await service.start()
        tasks = service._tasks[:]
        await service.stop()
        assert all(task.done() for task in tasks)
    run(scenario())


def test_env_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGHTNESS_DRIVER", "simulated")
    monkeypatch.setenv("BRIGHTNESS_STATE_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("BRIGHTNESS_LOCATION_OVERRIDE", AMSTERDAM.model_dump_json())
    service = controller_from_env()
    assert service.driver.name == "simulated"
    assert service.location() == (AMSTERDAM, "config_override")
    monkeypatch.setenv("BRIGHTNESS_DRIVER", "invalid")
    with pytest.raises(ValueError):
        controller_from_env()


def test_diagnostics_are_read_only_and_handle_missing_hardware(tmp_path):
    panel = device(tmp_path)
    original = {path: path.read_bytes() for path in panel.iterdir()}
    report = diagnostics(tmp_path, tmp_path / "absent-drm")
    assert report["read_only"]
    assert report["backlights"][0]["attributes"]["brightness"]["value"] == "128"
    assert original == {path: path.read_bytes() for path in panel.iterdir()}
    assert diagnostics(tmp_path / "absent", tmp_path / "absent")["backlights"] == []
