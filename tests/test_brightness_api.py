import asyncio
from unittest.mock import AsyncMock

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main as kiosk
from brightness import BrightnessController, Location
from conftest import load_remote


def test_kiosk_api_validation_persistence_and_software_level(tmp_path, monkeypatch):
    service = BrightnessController(tmp_path / "brightness.json",
                                   location_override=Location(latitude=52, longitude=5, timezone="Europe/Amsterdam"))
    monkeypatch.setattr(kiosk, "_brightness", service)
    client = TestClient(kiosk.app)  # No lifespan: never touch the real playlist or hardware.
    response = client.put("/api/brightness", json={"action": "manual", "manual_percent": 45})
    assert response.status_code == 200
    assert response.json()["method"] == "software"
    assert response.json()["level_percent"] == 45
    assert (tmp_path / "brightness.json").is_file()
    assert client.get("/api/brightness").json()["mode"] == "override"
    assert client.put("/api/brightness", json={"manual_percent": 45}).status_code == 422
    assert client.put("/api/brightness", json={"day_percent": 101}).status_code == 422
    assert client.put("/api/brightness", json={"enabled": False}).json()["mode"] == "disabled"
    assert client.put("/api/brightness", json={"action": "resume"}).json()["mode"] == "automatic"


def test_kiosk_not_initialized_and_persistence_failure(tmp_path, monkeypatch):
    client = TestClient(kiosk.app)
    monkeypatch.setattr(kiosk, "_brightness", None)
    assert client.get("/api/brightness").status_code == 503
    service = BrightnessController(tmp_path / "brightness.json")
    monkeypatch.setattr(kiosk, "_brightness", service)
    monkeypatch.setattr(service, "_persist", lambda state: (_ for _ in ()).throw(PermissionError("denied")))
    assert client.put("/api/brightness", json={"enabled": False}).status_code == 503


def test_kiosk_lifecycle_starts_and_stops_controller(tmp_path, monkeypatch):
    async def scenario():
        fake = AsyncMock()
        monkeypatch.setattr(kiosk, "controller_from_env", lambda **kwargs: fake)
        monkeypatch.setattr(kiosk, "_load_playlist_state", lambda: None)
        await kiosk.startup()
        fake.start.assert_awaited_once()
        assert kiosk._brightness is fake
        await kiosk.shutdown()
        fake.stop.assert_awaited_once()
        assert kiosk._brightness is None
    asyncio.run(scenario())


def test_bad_brightness_config_does_not_stop_kiosk(monkeypatch):
    async def scenario():
        monkeypatch.setattr(kiosk, "_load_playlist_state", lambda: None)
        monkeypatch.setattr(kiosk, "controller_from_env", lambda **kwargs: (_ for _ in ()).throw(ValueError("bad config")))
        await kiosk.startup()
        assert kiosk._slideshow_task is not None
        assert kiosk._brightness is None
        assert "bad config" in kiosk._brightness_init_error
        await kiosk.shutdown()
    asyncio.run(scenario())


def test_remote_routes_only_to_own_frame_and_preserves_settings(monkeypatch):
    remote = load_remote()
    get = AsyncMock(return_value={"mode": "automatic"})
    put = AsyncMock(return_value={"mode": "override", "level_percent": 35})
    monkeypatch.setattr(remote, "frame_get", get)
    monkeypatch.setattr(remote, "frame_put", put)
    client = TestClient(remote.app)
    assert client.get("/api/adam/brightness").status_code == 200
    get.assert_awaited_once_with("adam", "/brightness")
    response = client.put("/api/steve/brightness", json={"action": "manual", "manual_percent": 35})
    assert response.json()["level_percent"] == 35
    put.assert_awaited_once_with("steve", "/brightness", json={"action": "manual", "manual_percent": 35})
    assert client.get("/api/unknown/brightness").status_code == 404
    assert client.put("/api/adam/brightness", json={"day_percent": 101}).status_code == 422
    assert client.put("/api/adam/brightness", json={"action": "manual"}).status_code == 422
    assert client.put("/api/adam/brightness", json={"action": "resume", "enabled": False}).status_code == 422
    assert client.put("/api/adam/brightness", json={"device": "/tmp/other"}).status_code == 422


def test_remote_offline_frame_and_error_status(monkeypatch):
    remote = load_remote()

    async def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.AsyncClient, "request", offline)
    # Starlette TestClient is synchronous, so its requests do not use this AsyncClient method.
    client = TestClient(remote.app)
    response = client.get("/api/adam/brightness")
    assert response.status_code == 502
    assert "offline" in response.json()["detail"]


def test_remote_software_status_is_forwarded(monkeypatch):
    remote = load_remote()
    status = {"level_percent": 35, "method": "software", "errors": {"location": "offline"}}
    monkeypatch.setattr(remote, "frame_get", AsyncMock(return_value=status))
    assert TestClient(remote.app).get("/api/steve/brightness").json() == status


def test_remote_combined_status_contains_own_brightness_and_survives_failure(monkeypatch):
    remote = load_remote()

    async def get(user, path):
        if path == "/brightness":
            raise HTTPException(502, "unavailable")
        return {"volume_percent": 40} if path == "/volume" else {"state": "idle", "call": None}

    monkeypatch.setattr(remote, "frame_get", get)
    result = TestClient(remote.app).get("/api/adam/status").json()
    assert "unavailable" in result["self"]["brightness"]["error"]
    assert result["self"]["volume"]["volume_percent"] == 40
    assert "brightness" not in result["other"]


def test_remote_page_contains_accessible_brightness_controls():
    remote = load_remote()
    response = TestClient(remote.app).get("/adam")
    assert response.status_code == 200
    assert 'id="brightnessSlider"' in response.text
    assert 'for="brightnessSlider"' in response.text
    assert '/static/brightness.js' in response.text
    assert 'id="locationCard"' in response.text
    assert 'id="locationTimezone"' in response.text
    assert 'id="radioTimezone"' not in response.text


def test_kiosk_html_bootstraps_brightness_without_browser_storage(tmp_path, monkeypatch):
    async def setup():
        service = BrightnessController(tmp_path / "brightness.json")
        await service.update(__import__("brightness").BrightnessUpdate(action="manual", manual_percent=25))
        return service
    monkeypatch.setattr(kiosk, "_brightness", asyncio.run(setup()))
    client = TestClient(kiosk.app)
    for url in ("/", "/index.html", "/kiosk"):
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert 'id="brightness-bootstrap">{"level_percent": 25, "transition_seconds": 0}' in response.text
        assert "latitude" not in response.text


def test_new_sse_connection_receives_current_level_and_later_changes(tmp_path, monkeypatch):
    from brightness import BrightnessUpdate
    async def scenario():
        service = BrightnessController(tmp_path / "brightness.json",
            on_change=lambda snapshot: kiosk.sse_send("brightness", snapshot))
        monkeypatch.setattr(kiosk, "_brightness", service)
        await service.update(BrightnessUpdate(action="manual", manual_percent=40))
        response = await kiosk.events()
        stream = response.body_iterator
        assert '"level_percent": 40' in await anext(stream)
        await service.update(BrightnessUpdate(action="manual", manual_percent=25))
        event = await anext(stream)
        assert '"event": "brightness"' in event
        assert '"level_percent": 25' in event
        await stream.aclose()
        assert kiosk._subs == []
    asyncio.run(scenario())
