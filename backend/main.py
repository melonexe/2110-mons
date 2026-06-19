"""
FastAPI backend for the AES67 / SMPTE 2110 audio monitor.
Serves meter data and audio via WebSocket, REST API, and static frontend.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from audio_monitor import AudioMonitor, MonitorConfig, list_alsa_devices
from audio_streamer import AudioStreamer
from sap_listener import SAPListener
from sdp_parser import parse_sdp
import ravenna_api

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

monitor        = AudioMonitor()
audio_streamer = AudioStreamer()
sap            = SAPListener()
ws_clients:    Set[WebSocket] = set()

FRONTEND_DIR      = Path(__file__).parent.parent / "frontend"
PUSH_INTERVAL_MS  = 40  # meter WebSocket push rate (25 fps)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Wire audio monitor → audio streamer
    monitor.set_audio_callback(audio_streamer.feed)

    sap.start()

    # Background task: push audio frames to browser clients
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


# ── REST — SDP subscription ───────────────────────────────────────────────────

@app.post("/api/sdp/parse")
async def api_sdp_parse(request: Request):
    """Parse an SDP body and return the structured stream info (no subscription)."""
    sdp_text = (await request.body()).decode("utf-8", errors="replace").strip()
    if not sdp_text:
        return JSONResponse({"error": "Empty SDP body."}, status_code=400)
    parsed = parse_sdp(sdp_text)
    return parsed.to_dict()


@app.post("/api/sdp/subscribe")
async def api_sdp_subscribe(request: Request):
    """
    Parse the SDP in the request body, then ask the Merging RAVENNA daemon
    to subscribe to the primary stream.  Returns subscription details and
    an ALSA device hint so the UI can auto-start monitoring.
    """
    sdp_text = (await request.body()).decode("utf-8", errors="replace").strip()
    if not sdp_text:
        return JSONResponse({"error": "Empty SDP body."}, status_code=400)

    parsed = parse_sdp(sdp_text)
    if parsed.error:
        return JSONResponse({"error": parsed.error}, status_code=400)
    if parsed.primary is None:
        return JSONResponse({"error": "No audio m= section found in SDP."}, status_code=400)

    result = await ravenna_api.subscribe(sdp_text, parsed)
    return result


@app.get("/api/sdp/subscriptions")
async def api_sdp_list():
    return {"subscriptions": ravenna_api.list_subscriptions()}


@app.delete("/api/sdp/subscription/{sub_id}")
async def api_sdp_delete(sub_id: str):
    return await ravenna_api.unsubscribe(sub_id)


@app.get("/api/daemon/status")
async def api_daemon_status():
    """Health check: ping aes67-daemon and return PTP sync status."""
    ok, err = await ravenna_api._ping_daemon()
    ptp = await ravenna_api.get_ptp_status() if ok else {}
    return {
        "daemon_online": ok,
        "daemon_url": ravenna_api.DAEMON_URL,
        "error": err if not ok else None,
        "ptp": ptp,
        "sinks": await ravenna_api.get_daemon_sinks() if ok else [],
    }


# ── WebSocket — real-time meter data ──────────────────────────────────────────

@app.websocket("/ws/meters")
async def ws_meters(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    log.info("Meter WS client connected (%d total)", len(ws_clients))
    try:
        while True:
            state = monitor.get_state()
            state["streams"] = sap.get_streams()
            state["subscriptions"] = ravenna_api.list_subscriptions()
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
    """
    Binary WebSocket delivering interleaved float32 stereo PCM at the
    stream's native sample rate.  Audio is pushed by the background streamer
    task; this handler just keeps the connection alive.
    """
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
