from fastapi import FastAPI, UploadFile, File, HTTPException, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional, Dict, Any
import shutil, asyncio, subprocess, re, json, time, uuid, threading
from enum import Enum

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
    except subprocess.TimeoutExpired as e:
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

def push_call(session: Optional[CallSession], extra: Dict[str, Any] | None = None):
    payload = {"state": CallState.idle, "call": None}
    if session is not None:
        payload = {"state": session.state, "call": session.model_dump()}
    if extra:
        payload.update(extra)
    sse_send("call", payload)

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
    push_call(None, extra={"reason": "reset"})
    return {"ok": True}


#
# Reaction (maybe :)
#

class ReactionIn(BaseModel):
    message: str

@api.post("/reaction", status_code=202)
def reaction(body: ReactionIn):
    sse_send("reaction", body.model_dump())
    return {"ok": True}


#
# Upload a picture
#

ALLOWED = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

PICTURE_DIR = Path("pics")
PICTURE_DIR.mkdir(exist_ok=True)

app.mount("/pics", StaticFiles(directory=str(PICTURE_DIR)), name="pics")

@api.post("/picture", status_code=201)
async def upload_picture(file: UploadFile = File(...)):
    ext = ALLOWED.get(file.content_type)
    if not ext:
        raise HTTPException(415, "Unsupported image type")
    
    dst = PICTURE_DIR / f"current{ext}"
    tmp = PICTURE_DIR / f".upload_tmp{ext}"
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    tmp.replace(dst)
    
    sse_send("picture", {"url": f"/pics/current{ext}"})
    
    await file.close()
    return {"ok": True, "url": f"/pics/current{ext}"}


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
        # let /api/* be handled by your API routes
        if path.startswith("api/"):
            raise HTTPException(status_code=404)

        p = STATIC_DIR / path
        if p.is_file():
            return FileResponse(p)

        return FileResponse(STATIC_DIR / "index.html")
else:
    @app.get("/")
    async def root():
        return {"app": "Kiosk Backend, static file not found..."}
