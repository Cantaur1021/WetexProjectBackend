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
GPIO_PIN = 5                # OUTPUT (BCM numbering) for active-low pulse (your dispenser line)
ACTIVE_DURATION = 5.0       # Duration to keep pin active (seconds)

# Trigger input:
# You said "pin 28" — treating this as PHYSICAL pin 26 which is BCM 7 on the Pi header.
TRIGGER_PIN_BCM = 26         # INPUT (BCM numbering) watched for LOW to start dispense
DEBOUNCE_MS = 40            # Require ~40ms stable LOW
COOLDOWN_MS = 500           # Minimum time after LOW before re-arming
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
    """Activate GPIO_PIN (active low) for ACTIVE_DURATION seconds"""
    if not GPIO_AVAILABLE:
        print(f"[GPIO] Would activate pin {GPIO_PIN} for {ACTIVE_DURATION}s (GPIO not available)")
        return
    
    global GPIO_HANDLE
    if GPIO_HANDLE is None:
        print("[GPIO] GPIO not initialized")
        return
    
    try:
        print(f"[GPIO] Activating pin {GPIO_PIN} (active low) for {ACTIVE_DURATION} seconds")
        lgpio.gpio_write(GPIO_HANDLE, GPIO_PIN, 0)  # Active low
        await asyncio.sleep(ACTIVE_DURATION)
        lgpio.gpio_write(GPIO_HANDLE, GPIO_PIN, 1)  # Inactive high
        print(f"[GPIO] Pin {GPIO_PIN} deactivated (returned to high)")
    except Exception as e:
        print(f"[GPIO] Error controlling pin: {e}")

async def monitor_trigger_pin():
    """
    Watch TRIGGER_PIN_BCM for a LOW (falling-edge) with debounce.
    On valid trigger, broadcast StartDispense to all clients.
    """
    if not GPIO_AVAILABLE:
        print("[GPIO] Trigger monitor disabled (lgpio not available).")
        return
    global GPIO_HANDLE
    if GPIO_HANDLE is None:
        print("[GPIO] Trigger monitor disabled (GPIO not initialized).")
        return

    print(f"[GPIO] Monitoring trigger on BCM {TRIGGER_PIN_BCM} (active-low).")
    # Initial state
    try:
        last = lgpio.gpio_read(GPIO_HANDLE, TRIGGER_PIN_BCM)
    except Exception as e:
        print(f"[GPIO] Unable to read trigger pin: {e}")
        return

    while True:
        try:
            cur = lgpio.gpio_read(GPIO_HANDLE, TRIGGER_PIN_BCM)
        except Exception as e:
            print(f"[GPIO] Read error on trigger pin: {e}")
            await asyncio.sleep(0.1)
            continue

        # Detect HIGH->LOW transition
        if last == 1 and cur == 0:
            # Debounce: ensure it stays LOW for DEBOUNCE_MS
            await asyncio.sleep(DEBOUNCE_MS / 1000.0)
            try:
                stable = lgpio.gpio_read(GPIO_HANDLE, TRIGGER_PIN_BCM)
            except Exception as e:
                print(f"[GPIO] Read error (debounce): {e}")
                stable = 1

            if stable == 0:
                print(f"[GPIO] Trigger detected on BCM {TRIGGER_PIN_BCM} (LOW). Broadcasting StartDispense.")
                await manager.broadcast({"type": "StartDispense"})
                # Wait for release to HIGH
                while True:
                    try:
                        if lgpio.gpio_read(GPIO_HANDLE, TRIGGER_PIN_BCM) == 1:
                            break
                    except Exception:
                        break
                    await asyncio.sleep(0.01)
                # Cooldown
                await asyncio.sleep(COOLDOWN_MS / 1000.0)

        last = cur
        await asyncio.sleep(0.01)  # ~100 Hz polling

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
                # Trigger GPIO active low for ACTIVE_DURATION seconds
                asyncio.create_task(activate_gpio_pin())
            elif mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            else:
                print(f"[WS] Unknown message: {mtype}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ---------- Cross-platform stdin listener (works on Windows) ----------
# Left in place but no longer needed for 'A' trigger; the trigger is now the input pin.
async def input_listener():
    if not sys.stdin or not sys.stdin.isatty():
        print("[INPUT] Stdin not interactive; input listener disabled.")
        return

    print("[INPUT] Type 'N' + Enter to simulate NoChocolate (GPIO pulse).")

    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.1)
            continue
        cmd = line.strip().upper()
        if cmd == "N":
            print("[INPUT] Simulating NoChocolate")
            print("Moving robot back")
            asyncio.create_task(activate_gpio_pin())
        else:
            print(f"[INPUT] Unknown command: {cmd}")

# ---------- Launch the frontend in a browser/kiosk ----------
def _find_browser_cmd():
    """Return (cmd, args_list) best-suited for this OS."""
    for candidate in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable"):
        path = shutil.which(candidate)
        if path:
            if KIOSK:
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
    await asyncio.sleep(1.0)
    cmd = _find_browser_cmd()
    try:
        if cmd:
            print(f"[LAUNCH] Starting browser: {' '.join(cmd)}")
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
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
        
        # OUTPUT: dispenser line, initial HIGH (inactive)
        lgpio.gpio_claim_output(GPIO_HANDLE, GPIO_PIN, 1)

        # INPUT: trigger pin (BCM 7, phys pin 26) with internal pull-up
        lgpio.gpio_claim_input(GPIO_HANDLE, TRIGGER_PIN_BCM, lgpio.SET_PULL_UP)

        print(f"[GPIO] Output pin {GPIO_PIN} configured HIGH (inactive)")
        print(f"[GPIO] Trigger input configured on BCM {TRIGGER_PIN_BCM} with internal pull-up")
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
    setup_gpio()
    # Start listeners
    asyncio.create_task(input_listener())          # optional helper
    asyncio.create_task(launch_frontend())
    asyncio.create_task(monitor_trigger_pin())     # <-- NEW: auto StartDispense on pin LOW

@app.on_event("shutdown")
async def shutdown_event():
    cleanup_gpio()

if __name__ == "__main__":
    try:
        uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
    finally:
        cleanup_gpio()
