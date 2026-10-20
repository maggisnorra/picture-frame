import hashlib
import re
import shutil

from fastapi.testclient import TestClient

from conftest import load_remote


def test_remote_page_versions_every_local_script_and_stylesheet():
    remote = load_remote()
    client = TestClient(remote.app)
    for user in ("adam", "steve"):
        response = client.get(f"/{user}")
        assert response.headers["cache-control"] == "no-store"
        urls = re.findall(r'(?:src|href)="(/static/[^"?]+\.(?:js|css)\?v=[0-9a-f]{16})"', response.text)
        assert len(urls) == 4
        for url in urls:
            asset = client.get(url)
            assert asset.status_code == 200
            assert url.split("?v=")[1] == hashlib.sha256(asset.content).hexdigest()[:16]
            if "/radio.js?" in url:
                assert "if (timezoneHint)" in asset.text


def test_asset_url_changes_when_script_changes_not_just_when_server_restarts(tmp_path, monkeypatch):
    remote = load_remote()
    shutil.copytree(remote.BASE_DIR / "static", tmp_path / "static")
    monkeypatch.setattr(remote, "BASE_DIR", tmp_path)
    client = TestClient(remote.app)

    def script_url():
        return re.search(r'src="([^"]*radio\.js[^\"]*)"', client.get("/adam").text).group(1)

    original = script_url()
    assert script_url() == original
    script = tmp_path / "static/radio.js"
    script.write_text(script.read_text() + "\n// new release\n")
    assert script_url() != original
