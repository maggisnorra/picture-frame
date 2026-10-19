import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from brightness import (
    BrightnessController, BrightnessUpdate, CachedLocation, Location, Override,
    controller_from_env, lookup_location, solar_state,
)

UTC = timezone.utc
AMSTERDAM = Location(latitude=52.37, longitude=4.90, timezone="Europe/Amsterdam")
TROMSO = Location(latitude=69.65, longitude=18.96, timezone="Europe/Oslo")


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 10, 16, 12, tzinfo=UTC)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def make_controller(tmp_path, clock=None, **kwargs):
    clock = clock or Clock()
    options = dict(clock=lambda: clock.now, location_fallback=AMSTERDAM)
    options.update(kwargs)
    return BrightnessController(tmp_path / "brightness.json", **options), clock


def run(coro):
    return asyncio.run(coro)



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


def test_day_night_transitions_publish_browser_fades(tmp_path):
    async def scenario():
        events = []
        service, clock = make_controller(tmp_path, on_change=events.append)
        await service.tick()
        assert service.state.level_percent == 80
        assert events[-1] == {"level_percent": 80, "transition_seconds": 30}
        await service.update(BrightnessUpdate(action="manual", manual_percent=25))
        assert events[-1] == {"level_percent": 25, "transition_seconds": 0}
        await service.tick()
        assert len(events) == 2  # No repeated updates/fade restarts while the level is unchanged.
        await service.update(BrightnessUpdate(action="resume"))
        assert events[-1] == {"level_percent": 80, "transition_seconds": 30}
        clock.advance(12 * 3600)
        await service.tick()
        assert events[-1] == {"level_percent": 15, "transition_seconds": 30}
    run(scenario())


def test_manual_command_cancels_fade_even_at_same_destination(tmp_path):
    async def scenario():
        events = []
        service, _ = make_controller(tmp_path, on_change=events.append)
        await service.tick()
        assert events[-1] == {"level_percent": 80, "transition_seconds": 30}
        await service.update(BrightnessUpdate(action="manual", manual_percent=80))
        assert events[-1] == {"level_percent": 80, "transition_seconds": 0}
        assert len(events) == 2
    run(scenario())


def test_override_expiry_repeated_adjustments_and_restart(tmp_path):
    async def scenario():
        service, clock = make_controller(tmp_path)
        await service.update(BrightnessUpdate(action="manual", manual_percent=40))
        expires = service.state.override.expires_at
        clock.advance(60)
        await service.update(BrightnessUpdate(action="manual", manual_percent=35))
        assert service.state.override.expires_at == expires
        restored, _ = make_controller(tmp_path, clock)
        await restored.tick()
        assert restored.state.level_percent == 35
        clock.advance((expires - clock.now).total_seconds() + 1)
        await restored.tick()
        assert restored.state.override is None
        assert restored.state.level_percent == 15
        restarted, _ = make_controller(tmp_path, clock)
        await restarted.tick()
        assert restarted.state.override is None
        assert restarted.state.level_percent == 15
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
        assert service.state.level_percent == 100
        assert service.state.level_percent == 100
        assert (await service.status())["errors"]["schedule"]
        await service.update(BrightnessUpdate(action="manual", manual_percent=25))
        await service.update(BrightnessUpdate(enabled=False))
        clock.advance(86400)
        await service.tick()
        assert service.state.level_percent == 25
        assert (await service.status())["mode"] == "disabled"
        await service.update(BrightnessUpdate(action="resume"))
        assert service.state.level_percent == 25
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


def test_storage_failure_does_not_change_policy_or_publish(tmp_path):
    async def scenario():
        service, _ = make_controller(tmp_path)
        with patch("brightness.os.replace", side_effect=PermissionError("disk denied")):
            with pytest.raises(PermissionError):
                await service.update(BrightnessUpdate(action="manual", manual_percent=80))
        assert service.state.override is None
        assert service.state.level_percent == 100
        assert service.storage_error
        assert not list(tmp_path.iterdir())
    run(scenario())


def test_expired_override_cannot_survive_disk_error(tmp_path):
    async def scenario():
        service, clock = make_controller(tmp_path)
        service.state.override = Override(percent=35, expires_at=clock.now - timedelta(seconds=1))
        with patch.object(service, "_persist", side_effect=PermissionError("disk denied")):
            await service.tick()
        assert service.state.level_percent == 80
        assert service.state.level_percent == 80
    run(scenario())


def test_corrupt_state_disables_control(tmp_path):
    (tmp_path / "brightness.json").write_text("{bad json")
    async def scenario():
        service, _ = make_controller(tmp_path)
        await service.tick()
        assert service.state.level_percent == 100
        assert service.state.enabled is False
        assert service.storage_error
        await service.refresh_location()
        assert (tmp_path / "brightness.json").read_text() == "{bad json"
        assert service.storage_error
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
        assert result["level_percent"] == 35
        finish.set()
        await lookup
        assert service.state.override.percent == 35
    run(scenario())


def test_latest_manual_update_wins(tmp_path):
    async def scenario():
        events = []
        service, _ = make_controller(tmp_path, on_change=events.append)
        await asyncio.gather(*(service.update(BrightnessUpdate(action="manual", manual_percent=n)) for n in [60, 30, 75]))
        await service.tick()
        assert service.state.level_percent == 75
        assert events[-1]["level_percent"] == 75
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
    monkeypatch.setenv("BRIGHTNESS_STATE_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("BRIGHTNESS_LOCATION_OVERRIDE", AMSTERDAM.model_dump_json())
    service = controller_from_env()
    assert service.location() == (AMSTERDAM, "config_override")


def test_disabled_level_restored_and_old_hardware_settings_ignored(tmp_path, monkeypatch):
    async def scenario():
        service, _ = make_controller(tmp_path)
        await service.update(BrightnessUpdate(action="manual", manual_percent=35))
        await service.update(BrightnessUpdate(enabled=False))
        monkeypatch.setenv("BRIGHTNESS_DRIVER", "linux")
        monkeypatch.setenv("BRIGHTNESS_DEVICE", "/does/not/exist")
        monkeypatch.setenv("BRIGHTNESS_STATE_PATH", str(tmp_path / "brightness.json"))
        restored = controller_from_env()
        await restored.tick()
        status = await restored.status()
        assert status["method"] == "software"
        assert status["level_percent"] == 35
        assert status["mode"] == "disabled"
        assert "hardware" not in status["errors"]
        assert restored.display_snapshot(initial=True)["transition_seconds"] == 0
    run(scenario())


def test_previous_state_format_preserves_settings(tmp_path):
    (tmp_path / "brightness.json").write_text('{"enabled":false,"day_percent":65,"night_percent":20}')
    service, _ = make_controller(tmp_path)
    assert service.state.day_percent == 65
    assert service.state.night_percent == 20
    assert service.state.level_percent == 100
    assert service.state.enabled is False
