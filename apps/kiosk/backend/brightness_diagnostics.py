#!/usr/bin/env python3
"""Read-only brightness diagnostics; uses only the Python standard library."""

import json
import os
import platform
import stat
from pathlib import Path


def read(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError) as exc:
        return f"unavailable: {exc}"


def diagnostics(backlight_root=Path("/sys/class/backlight"), drm_root=Path("/sys/class/drm")):
    devices = []
    for device in sorted(backlight_root.glob("*")):
        attributes = {}
        for name in ("type", "max_brightness", "brightness", "actual_brightness", "bl_power"):
            path = device / name
            entry = {"value": read(path)}
            try:
                info = path.stat()
                entry.update(mode=stat.filemode(info.st_mode), uid=info.st_uid, gid=info.st_gid,
                             readable=os.access(path, os.R_OK), writable=os.access(path, os.W_OK))
            except OSError:
                pass
            attributes[name] = entry
        devices.append({"path": str(device), "resolved_path": str(device.resolve()), "attributes": attributes})
    connectors = []
    for path in sorted(drm_root.glob("*/status")):
        connectors.append({"name": path.parent.name, "status": read(path),
                           "enabled": read(path.parent / "enabled"), "modes": read(path.parent / "modes")})
    return {
        "read_only": True,
        "system": {"os": platform.system(), "kernel": platform.release(), "architecture": platform.machine(),
                   "model": read(Path("/proc/device-tree/model")).rstrip("\u0000"),
                   "uid": os.getuid(), "groups": os.getgroups()},
        "configured_driver": os.getenv("BRIGHTNESS_DRIVER", "linux"),
        "configured_device": os.getenv("BRIGHTNESS_DEVICE"),
        "backlights": devices, "connectors": connectors,
        "note": "No backlights means this version cannot control the physical backlight. Connector names alone do not prove dimming support.",
    }


if __name__ == "__main__":
    print(json.dumps(diagnostics(), indent=2))
