import asyncio
import json
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import main as kiosk
from chime import write_chime
from conftest import load_remote
from radio import MpvPlayer, RadioController, RadioSchedule, RadioSettings, active_radio as calculate_active_radio
from brightness import BrightnessController, BrightnessUpdate, CachedLocation, Location

UTC = timezone.utc
MONDAY = datetime(2026, 10, 19, 7, tzinfo=UTC)


REYKJAVIK = Location(latitude=64.14, longitude=-21.94, timezone="Atlantic/Reykjavik")
AMSTERDAM = Location(latitude=52.37, longitude=4.9, timezone="Europe/Amsterdam")


def active_radio(state, now, timezone_name="Atlantic/Reykjavik"):
    return calculate_active_radio(state, now, timezone_name)


def make_radio(path, player):
    return RadioController(path, player, location_provider=lambda: (REYKJAVIK, "ip_cache"))


def schedule(**changes):
    return RadioSchedule(**{
        "id": "monday", "start_date": "2026-10-19", "weekday": 0,
        "start_time": "07:00", "duration_minutes": 30, **changes,
    })


def settings(**changes):
    return RadioSettings(**{"schedules": [schedule()], **changes})


def fake_player():
    player = AsyncMock()
    player.playing = False
    return player


@pytest.mark.parametrize(("seconds", "factor", "ids"), [
    (-1, 0, []), (0, 0, ["monday"]), (60, 0.5, ["monday"]),
    (120, 1, ["monday"]), (900, 1, ["monday"]),
    (1680, 1, ["monday"]), (1740, 0.5, ["monday"]), (1800, 0, []),
])
def test_fade_boundaries(seconds, factor, ids):
    gain, active = active_radio(settings(), MONDAY + timedelta(seconds=seconds))
    assert gain == pytest.approx(factor)
    assert active == ids


def test_weekly_start_date_weekday_and_disabled():
    state = settings(schedules=[schedule(), schedule(id="tuesday", weekday=1, start_time="07:30", duration_minutes=25)])
    assert active_radio(state, MONDAY - timedelta(days=7))[1] == []
    assert active_radio(state, MONDAY + timedelta(days=7, minutes=5))[1] == ["monday"]
    assert active_radio(state, MONDAY + timedelta(days=1, minutes=35))[1] == ["tuesday"]
    assert active_radio(state, MONDAY + timedelta(days=1, minutes=55))[1] == []
    assert active_radio(settings(schedules=[schedule(enabled=False)]), MONDAY)[1] == []
    # Start date is a lower bound, even if it is not the selected weekday.
    state = settings(schedules=[schedule(start_date="2026-10-20")])
    assert active_radio(state, MONDAY)[1] == []
    assert active_radio(state, MONDAY + timedelta(days=7))[1] == ["monday"]


def test_cross_midnight_overlaps_and_short_windows():
    state = settings(schedules=[schedule(start_time="23:50")])
    now = datetime(2026, 10, 20, 0, 19, tzinfo=UTC)
    assert active_radio(state, now) == (0.5, ["monday"])
    state = settings(schedules=[schedule(), schedule(id="second", start_time="07:29")])
    assert active_radio(state, MONDAY + timedelta(minutes=29, seconds=30)) == (0.25, ["monday", "second"])
    assert active_radio(settings(schedules=[schedule(duration_minutes=1)]), MONDAY + timedelta(seconds=30))[0] == 0.25


def test_timezone_dst_skips_nonexistent_and_uses_first_ambiguous_time():
    state = settings(schedules=[schedule(weekday=6, start_date="2026-01-01", start_time="02:30", duration_minutes=30)])
    # 02:30 never exists on the spring transition.
    assert active_radio(state, datetime(2026, 3, 29, 1, 35, tzinfo=UTC), "Europe/Amsterdam")[1] == []
    assert active_radio(state, datetime(2026, 10, 25, 0, 35, tzinfo=UTC), "Europe/Amsterdam")[1] == ["monday"]
    assert active_radio(state, datetime(2026, 10, 25, 1, 35, tzinfo=UTC), "Europe/Amsterdam")[1] == []
    state = settings()
    assert active_radio(state, MONDAY - timedelta(hours=2) + timedelta(minutes=5), "Europe/Amsterdam")[0] == 1


@pytest.mark.parametrize("change", [
    {"timezone": "No/Such_Zone"}, {"station_url": "file:///etc/passwd"},
    {"schedules": [dict(schedule().model_dump(), weekday=7)]},
    {"schedules": [dict(schedule().model_dump(), start_time="24:00")]},
    {"schedules": [dict(schedule().model_dump(), duration_minutes=0)]},
    {"schedules": [dict(schedule().model_dump(), duration_minutes=1.5)]},
    {"schedules": [schedule(), schedule()]},
    {"schedules": [dict(schedule().model_dump(), start_date="invalid")]},
    {"unexpected": True},
])
def test_invalid_settings(change):
    with pytest.raises(ValidationError):
        settings(**change)


def test_persistence_restart_removal_and_save_failure(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "state/radio.json"
        controller = make_radio(path, fake_player())
        state = settings()
        await controller.update(state)
        restored = make_radio(path, fake_player())
        assert restored.settings == state
        await restored.step(MONDAY + timedelta(minutes=1))
        restored.player.apply.assert_awaited_once_with(str(state.station_url), 0.5)
        await restored.update(settings(schedules=[]))
        await restored.step(MONDAY + timedelta(minutes=2))
        restored.player.stop.assert_awaited_once()
        assert make_radio(path, fake_player()).settings.schedules == []
        monkeypatch.setattr(controller, "_persist", lambda state: (_ for _ in ()).throw(PermissionError("denied")))
        with pytest.raises(PermissionError):
            await controller.update(settings(schedules=[]))
        assert controller.settings == state
        assert "denied" in controller.storage_error
    asyncio.run(scenario())


def test_failed_player_retries_and_stops_outside_window(tmp_path):
    async def scenario():
        player = fake_player()
        player.apply.side_effect = FileNotFoundError("mpv missing")
        controller = make_radio(tmp_path / "radio.json", player)
        controller.settings = settings()
        await controller.step(MONDAY)
        assert "mpv missing" in controller.status()["error"]
        await controller.step(MONDAY + timedelta(seconds=1))
        assert player.apply.await_count == 1
        controller._retry_at = 0
        player.apply.side_effect = None
        await controller.step(MONDAY + timedelta(seconds=60))
        assert player.apply.await_count == 2
        assert controller.error is None
        await controller.step(MONDAY + timedelta(minutes=30))
        assert player.stop.await_count == 2
    asyncio.run(scenario())


def test_corrupt_storage_does_not_start_radio(tmp_path):
    path = tmp_path / "radio.json"
    path.write_text("bad json")
    controller = make_radio(path, fake_player())
    assert controller.settings.schedules == []
    assert controller.storage_error
    assert path.read_text() == "bad json"


def test_radio_uses_solar_location_and_follows_changes_even_with_brightness_disabled(tmp_path):
    async def scenario():
        brightness = BrightnessController(tmp_path / "brightness.json")
        brightness.state.enabled = False
        brightness.state.cached_location = CachedLocation(location=AMSTERDAM, fetched_at=MONDAY)
        player = fake_player()
        controller = RadioController(tmp_path / "radio.json", player, location_provider=brightness.location)
        controller.settings = settings()
        local_0701 = MONDAY - timedelta(hours=2) + timedelta(minutes=1)
        await controller.step(local_0701)
        player.apply.assert_awaited_once_with(str(controller.settings.station_url), 0.5)
        assert controller.status()["timezone"] == (await brightness.status())["location"]["timezone"]
        assert controller.status()["timezone_source"] == "ip_cache"
        # A new geolocation immediately changes schedule interpretation without resaving radio.
        brightness.state.cached_location = CachedLocation(location=REYKJAVIK, fetched_at=MONDAY)
        await controller.step(local_0701)
        player.stop.assert_awaited_once()
        assert controller.status()["timezone"] == "Atlantic/Reykjavik"
        assert "timezone" not in controller.settings.model_dump()
    asyncio.run(scenario())


def test_radio_waits_for_location_and_recovers_without_a_settings_update(tmp_path):
    async def scenario():
        brightness = BrightnessController(tmp_path / "brightness.json")
        player = fake_player()
        controller = RadioController(tmp_path / "radio.json", player, location_provider=brightness.location)
        controller.settings = settings()
        await controller.step(MONDAY + timedelta(minutes=1))
        player.apply.assert_not_awaited()
        assert controller.status()["timezone"] is None
        assert controller.status()["timezone_error"]
        brightness.state.cached_location = CachedLocation(location=REYKJAVIK, fetched_at=MONDAY)
        await controller.step(MONDAY + timedelta(minutes=1))
        player.apply.assert_awaited_once()
        assert controller.status()["timezone_error"] is None
    asyncio.run(scenario())


def test_radio_uses_same_override_fallback_and_offline_cache_as_solar_schedule(tmp_path):
    async def scenario():
        async def offline():
            raise httpx.ConnectError("offline")
        brightness = BrightnessController(tmp_path / "brightness.json", location_fallback=REYKJAVIK, locate=offline)
        controller = RadioController(tmp_path / "radio.json", fake_player(), location_provider=brightness.location)
        assert controller.status()["timezone_source"] == "config_fallback"
        brightness.state.cached_location = CachedLocation(location=AMSTERDAM, fetched_at=MONDAY - timedelta(days=3))
        await brightness.refresh_location()
        assert brightness.location_error
        assert controller.status()["timezone"] == "Europe/Amsterdam"
        assert controller.status()["timezone_source"] == "ip_cache"
        brightness.location_override = REYKJAVIK
        assert controller.status()["timezone"] == "Atlantic/Reykjavik"
        assert controller.status()["timezone_source"] == "config_override"
    asyncio.run(scenario())


def test_old_radio_timezone_is_ignored_while_schedules_and_station_survive(tmp_path):
    path = tmp_path / "radio.json"
    saved = settings().model_dump(mode="json")
    saved["timezone"] = "Europe/Amsterdam"
    path.write_text(json.dumps(saved))
    controller = make_radio(path, fake_player())
    assert controller.settings == settings()
    assert controller.status()["timezone"] == "Atlantic/Reykjavik"
    assert controller.storage_error is None
    asyncio.run(controller.update(controller.settings))
    assert "timezone" not in json.loads(path.read_text())


def test_kiosk_binds_radio_to_the_same_brightness_controller(tmp_path, monkeypatch):
    async def scenario():
        brightness = BrightnessController(tmp_path / "brightness.json", location_override=AMSTERDAM)
        monkeypatch.setattr(kiosk, "controller_from_env", lambda **kwargs: brightness)
        monkeypatch.setattr(kiosk, "_load_playlist_state", lambda: None)
        monkeypatch.setenv("RADIO_STATE_PATH", str(tmp_path / "radio.json"))
        await kiosk.startup()
        try:
            assert kiosk._radio.location_provider() == brightness.location()
            assert kiosk._radio.status()["timezone"] == "Europe/Amsterdam"
        finally:
            await kiosk.shutdown()
    asyncio.run(scenario())


def test_player_gain_is_linear_and_never_sets_speaker_volume(monkeypatch):
    async def scenario():
        player = MpvPlayer("test-speakers")
        player.process = type("Process", (), {"returncode": None})()
        player.url = "https://example.com/live"
        monkeypatch.setattr(player, "command", AsyncMock())
        for factor in (0, 0.125, 0.5, 1):
            await player.apply(player.url, factor)
            command, property_name, percent = player.command.call_args.args
            assert (command, property_name) == ("set_property", "volume")
            assert (percent / 100) ** 3 == pytest.approx(factor)
    asyncio.run(scenario())


def test_player_launches_silent_on_selected_sink_and_shuts_down(monkeypatch):
    async def scenario():
        player = MpvPlayer("test-speakers")
        process = type("Process", (), {"returncode": None, "terminate": lambda self: setattr(self, "returncode", 0), "wait": AsyncMock(return_value=0)})()
        calls = []

        async def spawn(*args, **kwargs):
            calls.append(args)
            socket = next(arg.split("=", 1)[1] for arg in args if arg.startswith("--input-ipc-server="))
            Path(socket).touch()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(player, "command", AsyncMock())
        await player.apply("https://example.com/live", 1)
        assert "--volume=0" in calls[0]
        assert "--audio-device=pulse/test-speakers" in calls[0]
        assert calls[0][-2:] == ("--", "https://example.com/live")
        await player.stop()
        assert player.process is None
        process.wait.assert_awaited_once()
    asyncio.run(scenario())


def test_kiosk_api_and_persistence_failure(tmp_path, monkeypatch):
    client = TestClient(kiosk.app)
    monkeypatch.setattr(kiosk, "_radio", None)
    assert client.get("/api/radio").status_code == 503
    controller = make_radio(tmp_path / "radio.json", fake_player())
    monkeypatch.setattr(kiosk, "_radio", controller)
    state = settings().model_dump(mode="json")
    assert client.put("/api/radio", json=state).status_code == 200
    assert client.get("/api/radio").json()["settings"] == state
    assert client.put("/api/radio", json={**state, "station_url": "javascript:alert(1)"}).status_code == 422
    monkeypatch.setattr(controller, "_persist", lambda state: (_ for _ in ()).throw(PermissionError("denied")))
    assert client.put("/api/radio", json=state).status_code == 503


def test_remote_radio_routes_only_to_own_frame_and_validates(monkeypatch):
    remote = load_remote()
    get = AsyncMock(return_value={"settings": settings().model_dump(mode="json")})
    put = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(remote, "frame_get", get)
    monkeypatch.setattr(remote, "frame_put", put)
    client = TestClient(remote.app)
    for user in ("adam", "steve"):
        assert client.get(f"/api/{user}/radio").status_code == 200
        get.assert_awaited_with(user, "/radio")
        body = settings().model_dump(mode="json")
        assert client.put(f"/api/{user}/radio", json=body).status_code == 200
        put.assert_awaited_with(user, "/radio", json=body)
        assert client.put(f"/api/{user}/radio", json={**body, "timezone": "Bad/Timezone"}).status_code == 422
    assert client.get("/api/unknown/radio").status_code == 404
    page = client.get("/adam").text
    assert 'for="radioStation"' in page
    assert '/static/radio.js' in page


def test_remote_radio_offline_error(monkeypatch):
    remote = load_remote()
    async def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx.AsyncClient, "request", offline)
    response = TestClient(remote.app).get("/api/adam/radio")
    assert response.status_code == 502
    assert "offline" in response.json()["detail"]


def test_reaction_chimes_and_uses_current_sink(monkeypatch):
    async def scenario():
        chime = AsyncMock()
        send = []
        monkeypatch.setattr(kiosk, "play_chime", chime)
        monkeypatch.setattr(kiosk, "sse_send", lambda *args: send.append(args))
        assert await kiosk.reaction(kiosk.ReactionIn(message="Hello")) == {"ok": True}
        await asyncio.gather(*list(kiosk._chime_tasks))
        chime.assert_awaited_once_with(kiosk.SOUND_SINK)
        assert send == [("reaction", {"message": "Hello"})]
    asyncio.run(scenario())


def test_local_chime_is_a_short_valid_wav(tmp_path):
    path = tmp_path / "message.wav"
    write_chime(path)
    with wave.open(str(path), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getnframes() / audio.getframerate() == pytest.approx(0.9)
