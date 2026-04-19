import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Literal, Optional
import uuid

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


User = Literal["adam", "steve"]
BASE_DIR = Path(__file__).parent

ADAM_FRAME_API = os.getenv("ADAM_FRAME_API", "https://frame-adam.maggisnorra.is/api")
STEVE_FRAME_API = os.getenv("STEVE_FRAME_API", "https://frame-steve.maggisnorra.is/api")

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


class SlideshowIn(BaseModel):
    interval_seconds: int = Field(ge=5, le=3600)


def other(u: User) -> User:
    return "steve" if u == "adam" else "adam"


def _ensure_user(u: str) -> User:
    if u not in ("adam", "steve"):
        raise HTTPException(404, f"Unknown user, got {u}")
    return u  # type: ignore


def _frame_url(u: User, path: str) -> str:
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{FRAMES[u]}{suffix}"


async def _frame_request(
    u: User,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    files: Any = None,
    timeout: float = 10,
) -> httpx.Response:
    url = _frame_url(u, path)
    headers = FRAME_HEADERS[u]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, headers=headers, json=json_body, files=files)
    except httpx.RequestError as e:
        raise HTTPException(502, f"Frame {u} {method} {path} failed: {str(e)}")


def _raise_frame_error(u: User, method: str, path: str, response: httpx.Response):
    if response.is_success:
        return
    preview = (response.text or "")[:300]
    raise HTTPException(502, f"Frame {u} {method} {path} failed: {response.status_code} {preview}")


def _json_from_frame(u: User, method: str, path: str, response: httpx.Response) -> Any:
    _raise_frame_error(u, method, path, response)
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return {"ok": True, "raw": response.text}


async def frame_get(u: User, path: str) -> Any:
    response = await _frame_request(u, "GET", path, timeout=5)
    return _json_from_frame(u, "GET", path, response)


async def frame_post(u: User, path: str, *, json: Any = None, files: Any = None) -> Any:
    response = await _frame_request(u, "POST", path, json_body=json, files=files)
    return _json_from_frame(u, "POST", path, response)


async def frame_put(u: User, path: str, *, json: Any = None) -> Any:
    response = await _frame_request(u, "PUT", path, json_body=json)
    return _json_from_frame(u, "PUT", path, response)


async def frame_delete(u: User, path: str) -> Any:
    response = await _frame_request(u, "DELETE", path)
    return _json_from_frame(u, "DELETE", path, response)


async def frame_file_proxy(u: User, path: str) -> Response:
    response = await _frame_request(u, "GET", path, timeout=15)
    if response.status_code == 404:
        raise HTTPException(404, f"Frame {u} GET {path} failed: 404")
    _raise_frame_error(u, "GET", path, response)
    media_type = response.headers.get("content-type", "application/octet-stream")
    return Response(content=response.content, media_type=media_type)


# -------------------------
# UI
# -------------------------

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(BASE_DIR / "static" / "favicon.ico")


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

    me_vol = safe(frame_get(me, "/volume"), "self volume")
    them_vol = safe(frame_get(them, "/volume"), "other volume")
    me_call = safe(frame_get(me, "/call/state"), "self call")
    them_call = safe(frame_get(them, "/call/state"), "other call")

    me_vol_r, them_vol_r, me_call_r, them_call_r = await asyncio.gather(
        me_vol, them_vol, me_call, them_call
    )

    def normalize_call(x: Any) -> dict[str, Any]:
        if isinstance(x, dict) and "call" in x and "state" in x:
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


@app.post("/api/{self_user}/volume/{action}")
async def volume(self_user: str, action: Literal["raise", "lower", "mute"]):
    me = _ensure_user(self_user)
    return await frame_post(me, f"/volume/{action}")


@app.post("/api/{self_user}/reaction")
async def reaction(self_user: str, body: Dict[str, Any]):
    me = _ensure_user(self_user)
    them = other(me)
    msg = (body or {}).get("message")
    if not isinstance(msg, str) or not msg.strip():
        raise HTTPException(400, "message required")
    return await frame_post(them, "/reaction", json={"message": msg.strip()})


@app.post("/api/{self_user}/picture", status_code=201)
async def picture(self_user: str, file: UploadFile = File(...)):
    me = _ensure_user(self_user)
    them = other(me)

    data = await file.read()
    files = {
        "file": (
            file.filename or "upload",
            data,
            file.content_type or "application/octet-stream",
        )
    }
    return await frame_post(them, "/picture", files=files)


@app.get("/api/{self_user}/pictures")
async def pictures(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)
    return await frame_get(them, "/pictures")


@app.delete("/api/{self_user}/pictures/{picture_id}")
async def delete_picture(self_user: str, picture_id: str):
    me = _ensure_user(self_user)
    them = other(me)
    return await frame_delete(them, f"/pictures/{picture_id}")


@app.get("/api/{self_user}/pictures/{picture_id}/file")
async def picture_file(self_user: str, picture_id: str):
    me = _ensure_user(self_user)
    them = other(me)
    return await frame_file_proxy(them, f"/pictures/{picture_id}/file")


@app.get("/api/{self_user}/picture/meta")
async def picture_meta(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)
    return await frame_get(them, "/picture/meta")


@app.get("/api/{self_user}/picture")
async def picture_current_file(self_user: str):
    me = _ensure_user(self_user)
    them = other(me)
    return await frame_file_proxy(them, "/picture")


@app.get("/api/{self_user}/slideshow")
async def get_slideshow(self_user: str):
    me = _ensure_user(self_user)
    return await frame_get(me, "/slideshow")


@app.put("/api/{self_user}/slideshow")
async def put_slideshow(self_user: str, body: SlideshowIn):
    me = _ensure_user(self_user)
    return await frame_put(me, "/slideshow", json=body.model_dump())


# -------------------------
# Call orchestration
# -------------------------


def _extract_call_id(resp: Any) -> Optional[str]:
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
    call_id = uuid.uuid4().hex

    r1 = await frame_post(me, "/call/initiate", json={"call_id": call_id})
    extracted_call_id = _extract_call_id(r1) or call_id
    r2 = await frame_post(them, "/call/receive", json={"call_id": extracted_call_id})

    return {"ok": True, "call_id": extracted_call_id, "self": r1, "other": r2}


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
