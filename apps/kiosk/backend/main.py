from fastapi import FastAPI, UploadFile, File, Request

app = FastAPI(title="Kiosk Backend")

@app.get("/wifi")
async def get_wifi():
    # TODO: return a list of available WiFis
    pass

@app.post("/wifi")
async def connect_wifi():
    # TODO: connect to a specific WiFi
    pass

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

@app.post("/picture", status_code=201)
def upload_picture(request: Request, file: UploadFile = File(...)):
    # TODO: store picture and show it 
    return {"ok": True}

@app.get("/")
async def root():
    return {"app": "Kiosk Backend"}