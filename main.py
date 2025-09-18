from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import json
import asyncio
import sys

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"[WS] Client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"[WS] Client disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        dead = []
        for ws in list(self.active_connections):
            try:
                await ws.send_text(json.dumps(message))
            except Exception as e:
                print(f"[WS] Broadcast failed: {e}")
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

@app.get("/health")
async def health():
    return {"ok": True}

class Signal(BaseModel):
    type: str

@app.post("/signal")
async def signal(payload: Signal):
    print(f"[API] Received signal request: {payload.type}")
    await manager.broadcast({"type": payload.type})
    return {"sent": payload.type}

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                mtype = data.get("type")
            except Exception:
                mtype = raw

            # FRONTEND → BACKEND signals
            if mtype == "YesChocolate":
                print("Dispensing Chocolate")
            elif mtype == "NoChocolate":
                print("Moving robot back")
            elif mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            else:
                print(f"[WS] Unknown message: {mtype}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ---------- Cross-platform stdin listener (works on Windows) ----------
async def input_listener():
    # If stdin isn't interactive (e.g., service), skip listening
    if not sys.stdin or not sys.stdin.isatty():
        print("[INPUT] Stdin not interactive; input listener disabled.")
        return

    print("[INPUT] Type 'A' + Enter to trigger GreetingTransition "
          "(Y → YesChocolate, N → NoChocolate).")

    while True:
        # Run blocking readline in a thread
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.1)
            continue
        cmd = line.strip().upper()
        if cmd == "A":
            print("[INPUT] Triggering GreetingTransition")
            await manager.broadcast({"type": "GreetingTransition"})
        elif cmd == "Y":
            print("[INPUT] Simulating YesChocolate")
            print("Dispensing Chocolate")
            # (Optionally broadcast to FE if you want the browser to react)
            # await manager.broadcast({"type": "YesChocolate"})
        elif cmd == "N":
            print("[INPUT] Simulating NoChocolate")
            print("Moving robot back")
            # (Optionally broadcast to FE)
            # await manager.broadcast({"type": "NoChocolate"})
        else:
            print(f"[INPUT] Unknown command: {cmd}")

@app.on_event("startup")
async def startup_event():
    # Spawn the stdin listener in the background
    asyncio.create_task(input_listener())

if __name__ == "__main__":
    # On Windows this is fine with --reload as well
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
