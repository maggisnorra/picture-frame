from enum import Enum
from pathlib import Path
from typing import Any, Optional
import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
import uuid

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


app = FastAPI(title="Kiosk Backend")
api = APIRouter(prefix="/api")


#
# SSE
#

_subs: list[asyncio.Queue[str]] = []


def sse_send(event: str, data: Any = None):
    msg = json.dumps({"event": event, "data": data})
    for q in list(_subs):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


@api.get("/events")
async def events():
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
    _subs.append(q)

    async def gen():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _subs.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


#
# Volume
#

SOUND_SINK = "alsa_output.platform-soc_107c000000_sound.stereo-fallback"
VOLUME_STEP = 5


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=2)
        return (r.stdout or "").strip()
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, (e.stderr or str(e)).strip())
    except subprocess.TimeoutExpired:
        raise HTTPException(500, f"Command timed out: {' '.join(cmd)}")


def get_volume():
    out = _run(["pactl", "get-sink-volume", SOUND_SINK])
    m = re.search(r"(\d+)%", out)
    vol = int(m.group(1)) if m else None
    if vol is None:
        raise HTTPException(500, f"Could not parse volume: {out}")
    muted = "MUTED" in _run(["pactl", "get-sink-mute", SOUND_SINK])
    return vol, muted


def set_volume_clamped(new_percent: int):
    new_percent = max(0, min(100, new_percent))
    _run(["pactl", "set-sink-volume", SOUND_SINK, f"{new_percent}%"])
    if new_percent > 0:
        _run(["pactl", "set-sink-mute", SOUND_SINK, "0"])
    elif new_percent == 0:
        _run(["pactl", "set-sink-mute", SOUND_SINK, "1"])


@api.post("/volume/raise", status_code=202)
def volume_raise():
    vol, muted = get_volume()
    set_volume_clamped(vol + VOLUME_STEP)
    vol, muted = get_volume()
    sse_send("volume", {"volume_percent": vol, "muted": muted})
    return {"ok": True, "volume_percent": vol, "muted": muted}


@api.post("/volume/lower", status_code=202)
def volume_lower():
    vol, muted = get_volume()
    set_volume_clamped(vol - VOLUME_STEP)
    vol, muted = get_volume()
    sse_send("volume", {"volume_percent": vol, "muted": muted})
    return {"ok": True, "volume_percent": vol, "muted": muted}


@api.post("/volume/mute", status_code=202)
def volume_mute_toggle():
    _run(["pactl", "set-sink-mute", SOUND_SINK, "toggle"])
    vol, muted = get_volume()
    sse_send("volume", {"volume_percent": vol, "muted": muted})
    return {"ok": True, "volume_percent": vol, "muted": muted}


@api.get("/volume")
def volume_get():
    vol, muted = get_volume()
    return {"volume_percent": vol, "muted": muted}


#
# Calling
#


class CallState(str, Enum):
    idle = "idle"
    outgoing_ringing = "outgoing_ringing"
    incoming_ringing = "incoming_ringing"
    connecting = "connecting"
    in_call = "in_call"
    ended = "ended"


class CallSession(BaseModel):
    call_id: str
    state: CallState
    created_at: float = Field(default_factory=lambda: time.time())


class CallStartIn(BaseModel):
    call_id: str


class CallActionIn(BaseModel):
    call_id: str


_call_lock = threading.Lock()
_call: Optional[CallSession] = None


def push_call(session: Optional[CallSession], extra: dict[str, Any] | None = None):
    payload: dict[str, Any] = {"state": CallState.idle, "call": None}
    if session is not None:
        payload = {"state": session.state, "call": session.model_dump()}
    if extra:
        payload.update(extra)
    sse_send("call", payload)


def _call_blocks_slideshow() -> bool:
    with _call_lock:
        return _call is not None and _call.state not in (CallState.idle, CallState.ended)


@api.post("/call/initiate", status_code=202)
def call_initiate(body: CallStartIn):
    global _call
    with _call_lock:
        if _call and _call.state not in (CallState.idle, CallState.ended):
            raise HTTPException(409, "Already in a call flow")
        _call = CallSession(call_id=body.call_id, state=CallState.outgoing_ringing)

    push_call(_call)
    return {"ok": True, "call": _call.model_dump()}


@api.post("/call/receive", status_code=202)
def call_receive(body: CallStartIn):
    global _call
    with _call_lock:
        if _call and _call.state not in (CallState.idle, CallState.ended):
            raise HTTPException(409, "Busy")
        _call = CallSession(call_id=body.call_id, state=CallState.incoming_ringing)

    push_call(_call)
    return {"ok": True, "call": _call.model_dump()}


@api.post("/call/accept", status_code=202)
def call_accept(body: CallActionIn):
    global _call
    with _call_lock:
        if not _call or _call.call_id != body.call_id:
            raise HTTPException(404, "Unknown call_id")
        if _call.state != CallState.incoming_ringing:
            raise HTTPException(409, f"Cannot accept from state={_call.state}")
        _call.state = CallState.connecting

    push_call(_call)
    return {"ok": True, "call": _call.model_dump()}


@api.post("/call/decline", status_code=202)
def call_decline(body: CallActionIn):
    global _call
    with _call_lock:
        if not _call or _call.call_id != body.call_id:
            raise HTTPException(404, "Unknown call_id")
        if _call.state != CallState.incoming_ringing:
            raise HTTPException(409, f"Cannot decline from state={_call.state}")
        ended_call = _call
        _call = None

    _touch_slideshow_clock()
    push_call(None, extra={"reason": "declined", "ended_call_id": ended_call.call_id})
    return {"ok": True}


@api.post("/call/end", status_code=202)
def call_end(body: CallActionIn):
    global _call
    with _call_lock:
        if not _call or _call.call_id != body.call_id:
            raise HTTPException(404, "Unknown call_id")
        ended_call = _call
        _call = None

    _touch_slideshow_clock()
    push_call(None, extra={"reason": "ended", "ended_call_id": ended_call.call_id})
    return {"ok": True}


@api.get("/call/state")
def call_state():
    with _call_lock:
        if not _call:
            return {"state": CallState.idle, "call": None}
        return {"state": _call.state, "call": _call.model_dump()}


@api.post("/call/reset", status_code=202)
def call_reset():
    global _call
    with _call_lock:
        _call = None
    _touch_slideshow_clock()
    push_call(None, extra={"reason": "reset"})
    return {"ok": True}


#
# Reaction
#


class ReactionIn(BaseModel):
    message: str


@api.post("/reaction", status_code=202)
def reaction(body: ReactionIn):
    sse_send("reaction", body.model_dump())
    return {"ok": True}


#
# Pictures / slideshow
#

ALLOWED = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
DEFAULT_INTERVAL_SECONDS = 30
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 3600

PICTURE_DIR = Path(__file__).parent / "pics"
PICTURE_DIR.mkdir(exist_ok=True)
LEGACY_META_PATH = PICTURE_DIR / "current.json"
PLAYLIST_PATH = PICTURE_DIR / "playlist.json"

app.mount("/pics", StaticFiles(directory=str(PICTURE_DIR)), name="pics")


class PictureEntry(BaseModel):
    picture_id: str
    filename: str
    original_filename: str
    content_type: str
    uploaded_at: int


class PlaylistState(BaseModel):
    images: list[PictureEntry] = Field(default_factory=list)
    current_picture_id: str | None = None
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    last_switch_at: float = Field(default_factory=lambda: time.time())


class SlideshowIn(BaseModel):
    interval_seconds: int = Field(ge=MIN_INTERVAL_SECONDS, le=MAX_INTERVAL_SECONDS)


_playlist_lock = threading.Lock()
_playlist_state: PlaylistState | None = None
_slideshow_task: asyncio.Task[None] | None = None


def _picture_url(picture_id: str) -> str:
    return f"/api/pictures/{picture_id}/file"


def _guess_content_type(path: Path, hinted: str | None = None) -> str:
    if hinted in ALLOWED:
        return str(hinted)
    for content_type, ext in ALLOWED.items():
        if path.suffix.lower() == ext:
            return content_type
    return "application/octet-stream"


def _sanitize_original_filename(name: str | None, ext: str) -> str:
    raw = Path(name or f"picture{ext}").name
    return raw or f"picture{ext}"


def _load_legacy_meta() -> dict[str, Any] | None:
    if not LEGACY_META_PATH.is_file():
        return None
    try:
        raw = json.loads(LEGACY_META_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        return None
    return None


def _find_legacy_picture() -> tuple[Path, str, str] | None:
    meta = _load_legacy_meta()
    if meta and "filename" in meta:
        candidate = PICTURE_DIR / str(meta["filename"])
        if candidate.is_file():
            content_type = _guess_content_type(candidate, meta.get("content_type"))
            return candidate, content_type, candidate.name

    candidates: list[tuple[float, Path, str]] = []
    for content_type, ext in ALLOWED.items():
        candidate = PICTURE_DIR / f"current{ext}"
        if candidate.is_file():
            candidates.append((candidate.stat().st_mtime, candidate, content_type))
    if not candidates:
        return None
    _, path, content_type = max(candidates, key=lambda item: item[0])
    return path, content_type, path.name


def _persist_playlist_unlocked():
    state = _playlist_state
    if state is None:
        return
    PLAYLIST_PATH.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def _normalize_playlist_unlocked() -> bool:
    global _playlist_state
    state = _playlist_state
    if state is None:
        return False

    changed = False
    normalized_images: list[PictureEntry] = []
    seen_ids: set[str] = set()
    for image in state.images:
        if image.picture_id in seen_ids:
            changed = True
            continue
        if not (PICTURE_DIR / image.filename).is_file():
            changed = True
            continue
        normalized_images.append(image)
        seen_ids.add(image.picture_id)

    if len(normalized_images) != len(state.images):
        state.images = normalized_images

    if not (MIN_INTERVAL_SECONDS <= state.interval_seconds <= MAX_INTERVAL_SECONDS):
        state.interval_seconds = DEFAULT_INTERVAL_SECONDS
        changed = True

    valid_current_ids = {image.picture_id for image in state.images}
    if state.current_picture_id not in valid_current_ids:
        state.current_picture_id = state.images[0].picture_id if state.images else None
        changed = True

    if state.last_switch_at <= 0:
        state.last_switch_at = time.time()
        changed = True

    return changed


def _migrate_legacy_picture() -> PlaylistState:
    found = _find_legacy_picture()
    if not found:
        return PlaylistState()

    path, content_type, original_name = found
    ext = ALLOWED.get(content_type) or path.suffix.lower() or ".bin"
    picture_id = uuid.uuid4().hex
    stored_filename = f"{picture_id}{ext}"
    dst = PICTURE_DIR / stored_filename
    if path != dst:
        path.replace(dst)
    if LEGACY_META_PATH.exists():
        LEGACY_META_PATH.unlink(missing_ok=True)

    uploaded_at = int(time.time())
    return PlaylistState(
        images=[
            PictureEntry(
                picture_id=picture_id,
                filename=stored_filename,
                original_filename=_sanitize_original_filename(original_name, ext),
                content_type=content_type,
                uploaded_at=uploaded_at,
            )
        ],
        current_picture_id=picture_id,
        interval_seconds=DEFAULT_INTERVAL_SECONDS,
        last_switch_at=time.time(),
    )


def _load_playlist_state() -> PlaylistState:
    if PLAYLIST_PATH.is_file():
        try:
            state = PlaylistState.model_validate_json(PLAYLIST_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = PlaylistState()
    else:
        state = _migrate_legacy_picture()

    global _playlist_state
    _playlist_state = state
    if _normalize_playlist_unlocked() or not PLAYLIST_PATH.exists():
        _persist_playlist_unlocked()
    return _playlist_state


def _require_playlist_state() -> PlaylistState:
    global _playlist_state
    if _playlist_state is None:
        return _load_playlist_state()
    return _playlist_state


def _image_payload(image: PictureEntry) -> dict[str, Any]:
    return {
        "picture_id": image.picture_id,
        "filename": image.original_filename,
        "content_type": image.content_type,
        "uploaded_at": image.uploaded_at,
        "url": _picture_url(image.picture_id),
    }


def _current_entry_unlocked() -> PictureEntry | None:
    state = _require_playlist_state()
    current_id = state.current_picture_id
    if not current_id:
        return None
    for image in state.images:
        if image.picture_id == current_id:
            return image
    return None


def _current_picture_payload_unlocked() -> dict[str, Any]:
    image = _current_entry_unlocked()
    state = _require_playlist_state()
    if image is None:
        return {
            "empty": True,
            "picture_id": None,
            "filename": None,
            "content_type": None,
            "updated_at": int(state.last_switch_at),
            "url": None,
        }

    return {
        "empty": False,
        "picture_id": image.picture_id,
        "filename": image.original_filename,
        "content_type": image.content_type,
        "updated_at": int(state.last_switch_at),
        "url": _picture_url(image.picture_id),
    }


def _playlist_payload_unlocked() -> dict[str, Any]:
    state = _require_playlist_state()
    return {
        "images": [_image_payload(image) for image in state.images],
        "current_picture_id": state.current_picture_id,
        "interval_seconds": state.interval_seconds,
        "empty": len(state.images) == 0,
    }


def _touch_slideshow_clock():
    with _playlist_lock:
        state = _require_playlist_state()
        state.last_switch_at = time.time()
        _persist_playlist_unlocked()


def _advance_picture_unlocked() -> dict[str, Any] | None:
    state = _require_playlist_state()
    if len(state.images) < 2:
        return None

    current_idx = 0
    if state.current_picture_id:
        for idx, image in enumerate(state.images):
            if image.picture_id == state.current_picture_id:
                current_idx = idx
                break

    next_idx = (current_idx + 1) % len(state.images)
    state.current_picture_id = state.images[next_idx].picture_id
    state.last_switch_at = time.time()
    _persist_playlist_unlocked()
    return _current_picture_payload_unlocked()


async def _slideshow_worker():
    while True:
        await asyncio.sleep(1)
        if _call_blocks_slideshow():
            continue

        snapshot: dict[str, Any] | None = None
        with _playlist_lock:
            state = _require_playlist_state()
            if len(state.images) < 2:
                continue
            if time.time() - state.last_switch_at < state.interval_seconds:
                continue
            snapshot = _advance_picture_unlocked()

        if snapshot is not None:
            sse_send("picture", snapshot)


@app.on_event("startup")
async def startup():
    global _slideshow_task
    with _playlist_lock:
        _load_playlist_state()
    _slideshow_task = asyncio.create_task(_slideshow_worker())


@app.on_event("shutdown")
async def shutdown():
    global _slideshow_task
    if _slideshow_task is None:
        return
    _slideshow_task.cancel()
    try:
        await _slideshow_task
    except asyncio.CancelledError:
        pass
    _slideshow_task = None


@api.post("/picture", status_code=201)
async def upload_picture(file: UploadFile = File(...)):
    content_type = file.content_type or ""
    ext = ALLOWED.get(content_type)
    if not ext:
        raise HTTPException(415, "Unsupported image type")

    picture_id = uuid.uuid4().hex
    stored_filename = f"{picture_id}{ext}"
    tmp = PICTURE_DIR / f".upload_{uuid.uuid4().hex}{ext}"
    dst = PICTURE_DIR / stored_filename
    original_filename = _sanitize_original_filename(file.filename, ext)
    uploaded_at = int(time.time())

    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        tmp.replace(dst)

        snapshot_to_send: dict[str, Any] | None = None
        with _playlist_lock:
            state = _require_playlist_state()
            image = PictureEntry(
                picture_id=picture_id,
                filename=stored_filename,
                original_filename=original_filename,
                content_type=content_type,
                uploaded_at=uploaded_at,
            )
            state.images.append(image)
            if not state.current_picture_id:
                state.current_picture_id = image.picture_id
                state.last_switch_at = time.time()
                snapshot_to_send = _current_picture_payload_unlocked()
            _persist_playlist_unlocked()
            payload = _image_payload(image)
            current_picture_id = state.current_picture_id

        if snapshot_to_send is not None:
            sse_send("picture", snapshot_to_send)

        return {
            "ok": True,
            "picture": payload,
            "current_picture_id": current_picture_id,
        }
    finally:
        await file.close()
        if tmp.exists():
            tmp.unlink(missing_ok=True)


@api.get("/pictures")
def list_pictures():
    with _playlist_lock:
        return _playlist_payload_unlocked()


@api.get("/pictures/{picture_id}/file")
def get_picture_by_id(picture_id: str):
    with _playlist_lock:
        state = _require_playlist_state()
        for image in state.images:
            if image.picture_id == picture_id:
                path = PICTURE_DIR / image.filename
                if not path.is_file():
                    raise HTTPException(404, "Picture file missing")
                return FileResponse(path, media_type=image.content_type)
    raise HTTPException(404, "Unknown picture_id")


@api.delete("/pictures/{picture_id}")
def delete_picture(picture_id: str):
    snapshot_to_send: dict[str, Any] | None = None
    with _playlist_lock:
        state = _require_playlist_state()
        idx = next((i for i, image in enumerate(state.images) if image.picture_id == picture_id), None)
        if idx is None:
            raise HTTPException(404, "Unknown picture_id")

        was_current = state.current_picture_id == picture_id
        image = state.images.pop(idx)
        (PICTURE_DIR / image.filename).unlink(missing_ok=True)

        if not state.images:
            state.current_picture_id = None
            state.last_switch_at = time.time()
            snapshot_to_send = _current_picture_payload_unlocked()
        elif was_current:
            next_idx = idx if idx < len(state.images) else 0
            state.current_picture_id = state.images[next_idx].picture_id
            state.last_switch_at = time.time()
            snapshot_to_send = _current_picture_payload_unlocked()
        elif state.current_picture_id not in {entry.picture_id for entry in state.images}:
            state.current_picture_id = state.images[0].picture_id
            state.last_switch_at = time.time()
            snapshot_to_send = _current_picture_payload_unlocked()

        _persist_playlist_unlocked()
        response = {
            "ok": True,
            "deleted_picture_id": picture_id,
            "current_picture_id": state.current_picture_id,
            "empty": len(state.images) == 0,
        }

    if snapshot_to_send is not None:
        sse_send("picture", snapshot_to_send)
    return response


@api.get("/slideshow")
def get_slideshow_settings():
    with _playlist_lock:
        state = _require_playlist_state()
        return {"interval_seconds": state.interval_seconds}


@api.put("/slideshow")
def set_slideshow_settings(body: SlideshowIn):
    with _playlist_lock:
        state = _require_playlist_state()
        state.interval_seconds = body.interval_seconds
        state.last_switch_at = time.time()
        _persist_playlist_unlocked()
        return {"ok": True, "interval_seconds": state.interval_seconds}


@api.get("/picture/meta")
def get_picture_meta():
    with _playlist_lock:
        return _current_picture_payload_unlocked()


@api.get("/picture")
def get_picture_file():
    with _playlist_lock:
        image = _current_entry_unlocked()
        if image is None:
            raise HTTPException(404, "No picture set")
        path = PICTURE_DIR / image.filename
        if not path.is_file():
            raise HTTPException(404, "Picture file missing")
        return FileResponse(path, media_type=image.content_type)


app.include_router(api)


#
# Serve React
#

STATIC_DIR = Path(__file__).parent / "static"

if STATIC_DIR.exists():

    @app.get("/")
    def spa_root():
        return FileResponse(STATIC_DIR / "index.html")


    @app.get("/{path:path}")
    def spa_fallback(path: str):
        if path.startswith("api/"):
            raise HTTPException(status_code=404)

        candidate = STATIC_DIR / path
        if candidate.is_file():
            return FileResponse(candidate)

        return FileResponse(STATIC_DIR / "index.html")

else:

    @app.get("/")
    async def root():
        return {"app": "Kiosk Backend, static file not found..."}
