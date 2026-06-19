"""
FastAPI backend for the AES67 / SMPTE 2110 audio monitor.
Serves meter data and audio via WebSocket, REST API, and static frontend.
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from audio_monitor import AudioMonitor, MonitorConfig, list_alsa_devices
from audio_streamer import AudioStreamer
from sap_listener import SAPListener
from sdp_parser import parse_sdp

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

monitor        = AudioMonitor()
audio_streamer = AudioStreamer()
sap            = SAPListener()
ws_clients:    Set[WebSocket] = set()

# Simple in-process subscription registry (keyed by sub_id string)
_subscriptions: Dict[str, dict] = {}

FRONTEND_DIR     = Path(__file__).parent.parent / "frontend"
PUSH_INTERVAL_MS = 40  # meter WebSocket push rate (25 fps)


@asynccontextmanager
async def lifespan(app: FastAPI):
    monitor.set_audio_callback(audio_streamer.feed)
    sap.start()
    streamer_task = asyncio.create_task(audio_streamer.run_forever())
    yield
    streamer_task.cancel()
    monitor.stop()
    sap.stop()


app = FastAPI(title="AES67 Monitor", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST — devices & monitor control ─────────────────────────────────────────

@app.get("/api/devices")
async def get_devices():
    return {"devices": list_alsa_devices()}


@app.get("/api/streams")
async def get_streams():
    return {"streams": sap.get_streams()}


@app.get("/api/state")
async def get_state():
    return monitor.get_state()


@app.post("/api/start")
async def start_monitor(config: dict):
    cfg = MonitorConfig(
        device=config.get("device", "default"),
        num_channels=int(config.get("num_channels", 2)),
        sample_rate=int(config.get("sample_rate", 48000)),
        period_frames=int(config.get("period_frames", 1024)),
    )
    monitor.start(cfg)
    return {"status": "started", "config": config}


@app.post("/api/stop")
async def stop_monitor():
    monitor.stop()
    return {"status": "stopped"}


@app.post("/api/reset-clip")
async def reset_clip():
    monitor.reset_clip()
    return {"status": "ok"}


@app.post("/api/reset-integrated")
async def reset_integrated():
    monitor.reset_integrated()
    return {"status": "ok"}


# ── REST — audio listen channel selection ─────────────────────────────────────

@app.post("/api/audio/pair")
async def set_audio_pair(body: dict):
    left  = int(body.get("left",  0))
    right = int(body.get("right", 1))
    audio_streamer.set_monitor_pair(left, right)
    return {"status": "ok", "pair": [left, right]}


# ── REST — SDP / RTP subscription ────────────────────────────────────────────

@app.post("/api/sdp/parse")
async def api_sdp_parse(request: Request):
    """Parse an SDP and return structured stream info without subscribing."""
    sdp_text = (await request.body()).decode("utf-8", errors="replace").strip()
    if not sdp_text:
        return JSONResponse({"error": "Empty SDP body."}, status_code=400)
    return parse_sdp(sdp_text).to_dict()


@app.post("/api/sdp/subscribe")
async def api_sdp_subscribe(request: Request):
    """
    Parse the SDP and start receiving the primary AES67 stream via direct
    RTP multicast — no external driver or daemon required.
    """
    sdp_text = (await request.body()).decode("utf-8", errors="replace").strip()
    if not sdp_text:
        return JSONResponse({"error": "Empty SDP body."}, status_code=400)

    parsed = parse_sdp(sdp_text)
    if parsed.error:
        return JSONResponse({"error": parsed.error}, status_code=400)
    if parsed.primary is None:
        return JSONResponse({"error": "No audio m= section found in SDP."}, status_code=400)

    stream = parsed.primary

    # Start the RTP receiver — replaces any existing monitor session
    monitor.start_from_rtp(parsed)

    sub_id = f"sub_{parsed.session_id or int(time.time())}"
    _subscriptions[sub_id] = {
        "id":           sub_id,
        "session_name": parsed.session_name,
        "multicast":    stream.multicast_addr,
        "source":       stream.source_addr,
        "port":         stream.port,
        "encoding":     stream.encoding,
        "channels":     stream.channels,
        "sample_rate":  stream.sample_rate,
        "ptp_domain":   stream.ptp_domain,
        "redundant":    parsed.is_redundant,
    }

    return {
        "status":   "subscribed",
        "method":   "rtp-direct",
        "sub_id":   sub_id,
        "message": (
            f"Receiving '{parsed.session_name}' via RTP multicast "
            f"{stream.multicast_addr}:{stream.port}  "
            f"({stream.encoding}, {stream.channels} ch @ {stream.sample_rate} Hz)."
        ),
        "parsed": parsed.to_dict(),
    }


@app.get("/api/sdp/subscriptions")
async def api_sdp_list():
    return {"subscriptions": list(_subscriptions.values())}


@app.delete("/api/sdp/subscription/{sub_id}")
async def api_sdp_delete(sub_id: str):
    if sub_id not in _subscriptions:
        return JSONResponse({"error": f"Subscription '{sub_id}' not found."},
                            status_code=404)
    _subscriptions.pop(sub_id)
    monitor.stop()
    return {"status": "ok", "sub_id": sub_id}


# ── WebSocket — real-time meter data ──────────────────────────────────────────

@app.websocket("/ws/meters")
async def ws_meters(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    log.info("Meter WS client connected (%d total)", len(ws_clients))
    try:
        while True:
            state = monitor.get_state()
            state["streams"]       = sap.get_streams()
            state["subscriptions"] = list(_subscriptions.values())
            await ws.send_text(json.dumps(state))
            await asyncio.sleep(PUSH_INTERVAL_MS / 1000)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("Meter WS error: %s", e)
    finally:
        ws_clients.discard(ws)
        log.info("Meter WS client disconnected (%d total)", len(ws_clients))


# ── WebSocket — binary audio stream ───────────────────────────────────────────

@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    """Binary float32 stereo PCM pushed from the audio streamer background task."""
    await ws.accept()
    audio_streamer.add_client(ws)
    try:
        while True:
            await asyncio.sleep(5)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        audio_streamer.remove_client(ws)


# ── Serve frontend ────────────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/{path:path}")
    async def serve_static(path: str):
        file_path = FRONTEND_DIR / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(FRONTEND_DIR / "index.html"))


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
        reload=False,
    )
