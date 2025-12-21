from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pathlib import Path
import shutil, asyncio

app = FastAPI(title="Kiosk Backend")

@app.post("/volume/raise", status_code=202)
def volume_raise():
    # TODO: increase the volume by one
    return {"ok": True}

@app.post("/volume/lower", status_code=202)
def volume_lower():
    # TODO: lower the volume by one
    return {"ok": True}

@app.post("/call/initiate", status_code=202)
def call_initiate():
    # TODO: stop UI for calling
    return {"ok": True}

@app.post("/call/receive", status_code=202)
def call_receive():
    # TODO: show UI for receiving a call
    return {"ok": True}

@app.post("/call/accept", status_code=202)
def call_accept():
    # TODO: start an actual call
    return {"ok": True}

@app.post("/call/decline", status_code=202)
def call_decline():
    # TODO: stop showing the calling UI
    return {"ok": True}

@app.post("/call/end", status_code=202)
def call_end():
    # TODO: end ongoing call
    return {"ok": True}

@app.post("/reaction/{message}", status_code=202)
def reaction(message: str):
    # TODO: show the reaction
    return {"ok": True}


#
# Upload a picture
#

ALLOWED = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

PICTURE_DIR = Path("media")
PICTURE_DIR.mkdir(exist_ok=True)

app.mount("/media", StaticFiles(directory=str(PICTURE_DIR)), name="media")

@app.post("/picture", status_code=201)
async def upload_picture(file: UploadFile = File(...)):
    ext = ALLOWED.get(file.content_type)
    if not ext:
        raise HTTPException(415, "Unsupported image type")
    
    dst = PICTURE_DIR / f"current{ext}"
    tmp = PICTURE_DIR / f".upload_tmp{ext}"
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    tmp.replace(dst)
        
    for q in list(_subs):
        q.put_nowait("picture_updated")
    
    await file.close()
    return {"ok": True}

_subs: list[asyncio.Queue[str]] = []


#
# SEE
#

@app.get("/events")
async def events():
    q: asyncio.Queue[str] = asyncio.Queue()
    _subs.append(q)

    async def gen():
        try:
            while True:
                msg = await q.get()
                yield f"data: {msg}\n\n"
        finally:
            _subs.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


#
# Root
#

@app.get("/")
async def root():
    return {"app": "Kiosk Backend"}
