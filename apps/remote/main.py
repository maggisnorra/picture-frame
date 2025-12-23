import os
import asyncio
from typing import Literal, Optional, Dict, Any

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


User = Literal["adam", "steve"]

ADAM_FRAME_API = os.getenv("ADAM_FRAME_API", "https://adam-frame.maggisnorra.is/api")
STEVE_FRAME_API = os.getenv("STEVE_FRAME_API", "https://steve-frame.maggisnorra.is/api")

# Optional Cloudflare Access Service Token headers for server->frame calls
# (recommended once you lock down frame APIs)
ADAM_CF_ID = os.getenv("ADAM_CF_ACCESS_CLIENT_ID", "")
ADAM_CF_SECRET = os.getenv("ADAM_CF_ACCESS_CLIENT_SECRET", "")
STEVE_CF_ID = os.getenv("STEVE_CF_ACCESS_CLIENT_ID", "")
STEVE_CF_SECRET = os.getenv("STEVE_CF_ACCESS_CLIENT_SECRET", "")

FRAMES: dict[User, str] = {
    "adam": ADAM_FRAME_API.rstrip("/"),
    "steve": STEVE_FRAME_API.rstrip("/"),
}

FRAME_HEADERS: dict[User, Dict[str, str]] = {
    "adam": (
        {"CF-Access-Client-Id": ADAM_CF_ID, "CF-Access-Client-Secret": ADAM_CF_SECRET}
        if ADAM_CF_ID and ADAM_CF_SECRET
        else {}
    ),
    "steve": (
        {"CF-Access-Client-Id": STEVE_CF_ID, "CF-Access-Client-Secret": STEVE_CF_SECRET}
        if STEVE_CF_ID and STEVE_CF_SECRET
        else {}
    ),
}

app = FastAPI(title="Remote Controller (UI + API)")


def other(u: User) -> User:
    return "steve" if u == "adam" else "adam"


def _ensure_user(u: str) -> User:
    if u not in ("adam", "steve"):
        raise HTTPException(404, "Unknown user")
    return u  # type: ignore


async def frame_get(u: User, path: str) -> Any:
    url = f"{FRAMES[u]}{path}"
    headers = FRAME_HEADERS[u]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Frame {u} GET {path} failed: {e.response.status_code} {e.response.text[:300]}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Frame {u} GET {path} failed: {str(e)}")


async def frame_post(u: User, path: str, *, json: Any = None, files: Any = None) -> Any:
    url = f"{FRAMES[u]}{path}"
    headers = FRAME_HEADERS[u]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers=headers, json=json, files=files)
            r.raise_for_status()
            # some endpoints might return empty; handle both
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return {"ok": True, "raw": r.text}
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Frame {u} POST {path} failed: {e.response.status_code} {e.response.text[:300]}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Frame {u} POST {path} failed: {str(e)}")


# -------------------------
# UI
# -------------------------

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/{self_user}", response_class=HTMLResponse)
def controller_page(request: Request, self_user: User):
    return templates.TemplateResponse(
        "remote.html",
        {
            "request": request,
            "self_user": self_user,
            "other_user": other(self_user),
        },
    )


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <html><body style="font-family:system-ui;padding:20px">
      <h2>Remote Controller</h2>
      <p>Choose:</p>
      <ul>
        <li><a href="/adam">/adam</a></li>
        <li><a href="/steve">/steve</a></li>
      </ul>
      <p><a href="/api/health">/api/health</a></p>
    </body></html>
    """


# -------------------------
# API
# -------------------------

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "frames": {
            "adam": FRAMES["adam"],
            "steve": FRAMES["steve"],
        },
        "access_tokens_configured": {
            "adam": bool(FRAME_HEADERS["adam"]),
            "steve": bool(FRAME_HEADERS["steve"]),
        },
    }


@app.get("/api/{self_user}/status")
async def status(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)

    async def safe(coro, name: str):
        try:
            return await coro
        except HTTPException as e:
            return {"error": f"{name}: {e.detail}"}

    # pull a minimal snapshot from both frames
    me_vol = safe(frame_get(me, "/volume"), "self volume")
    them_vol = safe(frame_get(them, "/volume"), "other volume")
    me_call = safe(frame_get(me, "/call/state"), "self call")
    them_call = safe(frame_get(them, "/call/state"), "other call")

    me_vol_r, them_vol_r, me_call_r, them_call_r = await asyncio.gather(
        me_vol, them_vol, me_call, them_call
    )

    # normalize call response into a simple object for UI
    def normalize_call(x: Any) -> dict[str, Any]:
        if isinstance(x, dict) and "call" in x and "state" in x:
            # kiosk backend returns {"state": "...", "call": {...} or None}
            return {"state": x.get("state"), "call": x.get("call")}
        return {"raw": x}

    return {
        "self": {
            "volume": me_vol_r,
            "call_state": normalize_call(me_call_r).get("state"),
            "call": normalize_call(me_call_r).get("call"),
        },
        "other": {
            "volume": them_vol_r,
            "call_state": normalize_call(them_call_r).get("state"),
            "call": normalize_call(them_call_r).get("call"),
        },
    }


# Volume controls OTHER frame
@app.post("/api/{self_user}/volume/{action}")
async def volume(self_user: str, action: Literal["raise", "lower", "mute"]):
    me = _ensure_user(self_user)
    them = other(me)
    return await frame_post(them, f"/volume/{action}")


# Reaction to OTHER frame
@app.post("/api/{self_user}/reaction")
async def reaction(self_user: str, body: Dict[str, Any]):
    me = _ensure_user(self_user)
    them = other(me)
    msg = (body or {}).get("message")
    if not isinstance(msg, str) or not msg.strip():
        raise HTTPException(400, "message required")
    return await frame_post(them, "/reaction", json={"message": msg.strip()})


# Picture to OTHER frame
@app.post("/api/{self_user}/picture")
async def picture(self_user: str, file: UploadFile = File(...)):
    me = _ensure_user(self_user)
    them = other(me)

    content_type = file.content_type or "application/octet-stream"
    data = await file.read()
    await file.close()

    # forward file to target frame
    files = {"file": (file.filename or "upload", data, content_type)}
    return await frame_post(them, "/picture", files=files)


# -------------------------
# Call orchestration
# -------------------------

def _extract_call_id(resp: Any) -> Optional[str]:
    """
    Your kiosk /call/initiate returns {"ok": True, "call": {...}} in the latest version.
    Accept also might return {"call": {...}}. Handle both.
    """
    if isinstance(resp, dict):
        if "call_id" in resp and isinstance(resp["call_id"], str):
            return resp["call_id"]
        call = resp.get("call")
        if isinstance(call, dict) and isinstance(call.get("call_id"), str):
            return call["call_id"]
    return None


async def _get_frame_call_id(u: User) -> Optional[str]:
    st = await frame_get(u, "/call/state")
    if isinstance(st, dict):
        call = st.get("call")
        if isinstance(call, dict) and isinstance(call.get("call_id"), str):
            return call["call_id"]
    return None


@app.post("/api/{self_user}/call/initiate")
async def call_initiate(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)

    # 1) initiate on self frame (gets a call_id)
    r1 = await frame_post(me, "/call/initiate", json={})
    call_id = _extract_call_id(r1)
    if not call_id:
        raise HTTPException(502, f"Could not get call_id from {me} initiate: {r1}")

    # 2) trigger receive on other frame using SAME call_id
    r2 = await frame_post(them, "/call/receive", json={"call_id": call_id})

    return {"ok": True, "call_id": call_id, "self": r1, "other": r2}


@app.post("/api/{self_user}/call/accept")
async def call_accept(self_user: str):
    me = _ensure_user(self_user)
    call_id = await _get_frame_call_id(me)
    if not call_id:
        raise HTTPException(409, f"No active call on {me}")
    r = await frame_post(me, "/call/accept", json={"call_id": call_id})
    return {"ok": True, "call_id": call_id, "self": r}


@app.post("/api/{self_user}/call/decline")
async def call_decline(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)
    call_id = await _get_frame_call_id(me)
    if not call_id:
        raise HTTPException(409, f"No active call on {me}")

    r1 = await frame_post(me, "/call/decline", json={"call_id": call_id})
    # also clear the other side if it's ringing with same call_id
    try:
        r2 = await frame_post(them, "/call/end", json={"call_id": call_id})
    except HTTPException:
        r2 = {"ok": False, "note": "other frame end failed (may be idle)"}

    return {"ok": True, "call_id": call_id, "self": r1, "other": r2}


@app.post("/api/{self_user}/call/end")
async def call_end(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)

    call_id = await _get_frame_call_id(me)
    if not call_id:
        # maybe the other side has it
        call_id = await _get_frame_call_id(them)
    if not call_id:
        raise HTTPException(409, "No active call on either frame")

    r1 = await frame_post(me, "/call/end", json={"call_id": call_id})
    try:
        r2 = await frame_post(them, "/call/end", json={"call_id": call_id})
    except HTTPException:
        r2 = {"ok": False, "note": "other frame end failed (may already be idle)"}

    return {"ok": True, "call_id": call_id, "self": r1, "other": r2}


@app.post("/api/{self_user}/call/reset")
async def call_reset(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)
    r1 = await frame_post(me, "/call/reset", json={})
    r2 = await frame_post(them, "/call/reset", json={})
    return {"ok": True, "self": r1, "other": r2}
