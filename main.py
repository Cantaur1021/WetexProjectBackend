from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn
import json
import asyncio
import sys
import os
import shutil
import subprocess
from pathlib import Path
import webbrowser

# GPIO imports - handle gracefully if not on Raspberry Pi
try:
    import lgpio
    GPIO_AVAILABLE = True
    GPIO_HANDLE = None  # Will store the chip handle
except ImportError:
    GPIO_AVAILABLE = False
    print("[GPIO] lgpio not available - GPIO functionality disabled")

# ──────────────────────────────────────────────────────────────────────────────
# Config: where your frontend lives and what URL to open
# Put your index.html (and assets like video.mp4) in ./frontend next to this file
FRONTEND_DIR = Path(__file__).parent / "frontend"
INDEX_FILE = FRONTEND_DIR / "index.html"
APP_URL = "http://localhost:8000/"
AUTO_LAUNCH = True          # set False to disable auto-opening a browser
KIOSK = True                # True => try Chromium kiosk flags (best for Pi)

# GPIO Configuration
GPIO_PIN = 5                # GPIO pin number (BCM numbering)
ACTIVE_DURATION = 5.0       # Duration to keep pin active (seconds)
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the frontend
if not FRONTEND_DIR.exists():
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

@app.get("/")
async def root_index():
    # Serve index.html from FRONTEND_DIR
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE)
    # fallback message if index is missing
    return {"message": f"Place your index.html in {FRONTEND_DIR}"}

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

# GPIO Helper Functions
async def activate_gpio_pin():
    """Activate GPIO pin 5 (active low) for ACTIVE_DURATION seconds"""
    if not GPIO_AVAILABLE:
        print(f"[GPIO] Would activate pin {GPIO_PIN} for {ACTIVE_DURATION}s (GPIO not available)")
        return
    
    global GPIO_HANDLE
    if GPIO_HANDLE is None:
        print("[GPIO] GPIO not initialized")
        return
    
    try:
        print(f"[GPIO] Activating pin {GPIO_PIN} (active low) for {ACTIVE_DURATION} seconds")
        lgpio.gpio_write(GPIO_HANDLE, GPIO_PIN, 0)  # Active low signal (0 = LOW)
        await asyncio.sleep(ACTIVE_DURATION)
        lgpio.gpio_write(GPIO_HANDLE, GPIO_PIN, 1)  # Return to high (1 = HIGH, inactive)
        print(f"[GPIO] Pin {GPIO_PIN} deactivated (returned to high)")
    except Exception as e:
        print(f"[GPIO] Error controlling pin: {e}")

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
                # Trigger GPIO pin 5 active low for 5 seconds
                asyncio.create_task(activate_gpio_pin())
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
            # Optionally broadcast to FE if you want:
            # await manager.broadcast({"type": "YesChocolate"})
        elif cmd == "N":
            print("[INPUT] Simulating NoChocolate")
            print("Moving robot back")
            # Trigger GPIO pin 5 active low for 5 seconds
            asyncio.create_task(activate_gpio_pin())
            # Optionally broadcast to FE:
            # await manager.broadcast({"type": "NoChocolate"})
        else:
            print(f"[INPUT] Unknown command: {cmd}")

# ---------- Launch the frontend in a browser/kiosk ----------
def _find_browser_cmd():
    """Return (cmd, args_list) best-suited for this OS."""
    # Prefer Chromium kiosk on Raspberry Pi / Linux
    for candidate in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"):
        path = shutil.which(candidate)
        if path:
            if KIOSK:
                # Kiosk flags suitable for exhibits
                return [path,
                        "--kiosk",
                        f"--app={APP_URL}",
                        "--incognito",
                        "--noerrdialogs",
                        "--disable-restore-session-state",
                        "--disable-infobars",
                        "--autoplay-policy=no-user-gesture-required"]
            else:
                return [path, APP_URL]
    return None

async def launch_frontend():
    if not AUTO_LAUNCH:
        return
    # Small delay to ensure server is listening
    await asyncio.sleep(1.0)

    cmd = _find_browser_cmd()
    try:
        if cmd:
            print(f"[LAUNCH] Starting browser: {' '.join(cmd)}")
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Fallback to default system browser
            print("[LAUNCH] Chromium not found; opening default browser.")
            webbrowser.open(APP_URL, new=1, autoraise=True)
    except Exception as e:
        print(f"[LAUNCH] Failed to open browser: {e}")

def setup_gpio():
    """Initialize GPIO settings"""
    if not GPIO_AVAILABLE:
        return
    
    global GPIO_HANDLE
    try:
        # Open gpiochip0 (default for Raspberry Pi)
        GPIO_HANDLE = lgpio.gpiochip_open(0)
        
        # Set pin as output with initial value HIGH (1 = inactive)
        lgpio.gpio_claim_output(GPIO_HANDLE, GPIO_PIN, 1)
        
        print(f"[GPIO] Pin {GPIO_PIN} configured as output (initially high/inactive)")
        print(f"[GPIO] Using lgpio with chip handle: {GPIO_HANDLE}")
    except Exception as e:
        print(f"[GPIO] Failed to initialize: {e}")
        GPIO_HANDLE = None

def cleanup_gpio():
    """Clean up GPIO resources"""
    global GPIO_HANDLE
    if GPIO_AVAILABLE and GPIO_HANDLE is not None:
        try:
            lgpio.gpiochip_close(GPIO_HANDLE)
            GPIO_HANDLE = None
            print("[GPIO] Cleanup complete - chip handle closed")
        except Exception as e:
            print(f"[GPIO] Cleanup error: {e}")

@app.on_event("startup")
async def startup_event():
    # Initialize GPIO
    setup_gpio()
    # Start stdin listener and browser launcher
    asyncio.create_task(input_listener())
    asyncio.create_task(launch_frontend())

@app.on_event("shutdown")
async def shutdown_event():
    # Clean up GPIO on shutdown
    cleanup_gpio()

if __name__ == "__main__":
    try:
        # Run: uvicorn main:app --reload --host 0.0.0.0 --port 8000
        uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
    finally:
        # Ensure GPIO cleanup even if interrupted
        cleanup_gpio()