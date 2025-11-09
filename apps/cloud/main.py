from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from typing import Literal
from pydantic import BaseModel, Field
from pathlib import Path
import os, uuid

app = FastAPI(title="Cloud API")

DATA_DIR = Path(__file__).resolve().parent / "data" / "images"
DATA_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

app.mount("/media", StaticFiles(directory=DATA_DIR), name="media")

ADAM: Literal["adam"] = "adam"
STEVE: Literal["steve"] = "steve"

def other_partner(sender: Literal["adam", "steve"]) -> Literal["adam", "steve"]:
    return STEVE if sender == ADAM else ADAM

def save_picture_for(receiver: Literal["adam", "steve"], file: UploadFile) -> str:
    """Save file atomically as 'show_on_<receiver>.<ext>' and return filename."""
    if file.content_type not in ALLOWED:
        raise HTTPException(400, "Only JPEG/PNG/WebP are allowed")
    ext = ALLOWED[file.content_type]
    final = DATA_DIR / f"show_on_{receiver}{ext}"
    tmp = DATA_DIR / f".tmp_{uuid.uuid4().hex}{ext}"
    # write then atomic replace so the frame never serves a partial file
    tmp.write_bytes(file.file.read())
    os.replace(tmp, final)
    return final.name

@app.post("/{sender}/upload_picture", status_code=201)
async def upload_picture(sender: Literal["adam", "steve"], request: Request, file: UploadFile = File(...)):
    receiver = other_partner(sender)
    fname = save_picture_for(receiver, file)
    base = str(request.base_url).rstrip("/")
    return {"ok": True, "receiver": receiver, "url": f"{base}/media/{fname}"}
    
@app.post("/{sender}/send_reaction/{message}", status_code=202)
def send_reaction(sender: Literal["adam","steve"], message: str):
    # TODO: push to the receiver's frame (HTTP call or WebSocket)
    return {"queued": True, "to": other_partner(sender), "message": message}

@app.post("/{sender}/call", status_code=202)
def call(sender: Literal["adam","steve"]):
    # TODO: set call state to 'ringing' and notify both frames
    return {"queued": True, "caller": sender, "callee": other_partner(sender)}

@app.post("/{sender}/call/accept", status_code=202)
def call_accept(sender: Literal["adam","steve"]):
    # TODO: validate 'ringing' -> 'in_call' and notify both frames
    return {"ok": True}

@app.post("/{sender}/call/decline", status_code=202)
def call_decline(sender: Literal["adam","steve"]):
    # TODO: clear call state and notify both frames
    return {"ok": True}

# create tables in dev (in prod: Alembic migrations)
#@app.on_event("startup")
#def on_startup():
#    with engine.begin() as conn:
#        conn.execute(text("SELECT 1"))  # touch DB
#    Base.metadata.create_all(bind=engine)

@app.get("/")
async def root():
    return {"app": "Cloud"}
