"""
Interface with the aes67-linux-daemon REST API.
https://github.com/bondagit/aes67-linux-daemon

The daemon runs on the same host and exposes HTTP on port 9090 (configured
in daemon.conf — we use 9090 to avoid clashing with our own port 8080).

Sink = a stream we subscribe to and receive audio from.
After adding a sink, the daemon writes received audio into the ALSA loopback
device (hw:Loopback,0), which our monitor app reads from the capture side
(hw:Loopback,1 / alias "aes67").

Override the daemon URL via environment variable:
  export RAVENNA_API_URL=http://localhost:9090
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import httpx

from sdp_parser import ParsedSDP, MediaStream

log = logging.getLogger(__name__)

DAEMON_URL  = os.environ.get("RAVENNA_API_URL", "http://localhost:9090").rstrip("/")
ALSA_DEVICE = "hw:Loopback,1"   # capture side of the snd-aloop loopback

# ── In-process subscription registry ─────────────────────────────────────────

@dataclass
class Subscription:
    id: str
    daemon_sink_id: int             # numeric ID used by aes67-daemon
    session_name: str
    multicast_addr: str
    source_addr: str
    channels: int
    sample_rate: int
    encoding: str
    alsa_device: str = ALSA_DEVICE
    created_at: float = field(default_factory=time.time)


_subscriptions: Dict[str, Subscription] = {}
_next_sink_id: int = 0             # aes67-daemon sink IDs are small integers


def list_subscriptions() -> List[dict]:
    return [asdict(s) for s in _subscriptions.values()]


def get_subscription(sub_id: str) -> Optional[dict]:
    s = _subscriptions.get(sub_id)
    return asdict(s) if s else None


# ── Main API ──────────────────────────────────────────────────────────────────

async def subscribe(sdp_text: str, parsed: ParsedSDP) -> dict:
    """
    Add a sink to the aes67-linux-daemon for the primary stream in *parsed*.

    Returns a dict with:
      status:     "subscribed" | "error"
      sub_id:     our internal subscription ID
      alsa_hint:  ALSA device string to open for monitoring
      message:    human-readable result
      parsed:     the parsed SDP info
    """
    stream = parsed.primary
    if stream is None:
        return {"status": "error", "message": "No primary audio stream found in SDP."}

    global _next_sink_id
    sink_id = _next_sink_id
    _next_sink_id += 1

    # Check daemon is reachable first
    reachable, err = await _ping_daemon()
    if not reachable:
        return {
            "status": "error",
            "message": (
                f"Cannot reach aes67-daemon at {DAEMON_URL}. "
                f"Is it running? ({err})\n"
                "Start it: sudo systemctl start aes67-daemon\n"
                "Check logs: journalctl -u aes67-daemon"
            ),
            "parsed": parsed.to_dict(),
        }

    result = await _add_sink(sink_id, parsed.session_name, sdp_text)
    if not result["ok"]:
        return {
            "status": "error",
            "message": result["message"],
            "parsed": parsed.to_dict(),
        }

    sub_id = f"sub_{sink_id}_{int(time.time())}"
    sub = Subscription(
        id=sub_id,
        daemon_sink_id=sink_id,
        session_name=parsed.session_name,
        multicast_addr=stream.multicast_addr,
        source_addr=stream.source_addr,
        channels=stream.channels,
        sample_rate=stream.sample_rate,
        encoding=stream.encoding,
    )
    _subscriptions[sub_id] = sub

    return {
        "status": "subscribed",
        "method": "aes67-daemon",
        "sub_id": sub_id,
        "alsa_hint": ALSA_DEVICE,
        "message": (
            f"Subscribed to '{parsed.session_name}'. "
            f"Audio is now available on ALSA device '{ALSA_DEVICE}'. "
            f"Set channels to {stream.channels}, rate to {stream.sample_rate} Hz."
        ),
        "parsed": parsed.to_dict(),
    }


async def unsubscribe(sub_id: str) -> dict:
    sub = _subscriptions.pop(sub_id, None)
    if sub is None:
        return {"status": "error", "message": f"Subscription '{sub_id}' not found."}

    result = await _remove_sink(sub.daemon_sink_id)
    if result["ok"]:
        return {"status": "ok", "sub_id": sub_id}
    else:
        return {"status": "error", "message": result["message"]}


async def get_daemon_sources() -> List[dict]:
    """Return the list of AES67 sources the daemon has discovered via SAP."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{DAEMON_URL}/api/sources")
            if r.status_code == 200:
                return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        log.debug("get_daemon_sources failed: %s", e)
    return []


async def get_daemon_sinks() -> List[dict]:
    """Return the daemon's current sink list."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{DAEMON_URL}/api/sinks")
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
    except Exception as e:
        log.debug("get_daemon_sinks failed: %s", e)
    return []


async def get_ptp_status() -> dict:
    """Return the daemon's PTP synchronisation status."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{DAEMON_URL}/api/ptp")
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        log.debug("get_ptp_status failed: %s", e)
    return {"status": "unreachable"}


# ── Daemon interaction ────────────────────────────────────────────────────────

async def _ping_daemon() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{DAEMON_URL}/api/config")
            return r.status_code == 200, ""
    except httpx.ConnectError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


async def _add_sink(sink_id: int, name: str, sdp_text: str) -> dict:
    """
    PUT /api/sink/{id} on the aes67-linux-daemon to create a new sink.

    The daemon expects JSON with use_sdp=true and the SDP embedded as a string.
    ignore_refclk_gmid=true relaxes the PTP grandmaster identity check, which
    is useful when the daemon's own PTP lock doesn't perfectly match the
    announced GM clock ID (common in VirtualBox due to software timestamps).
    """
    body = {
        "name": name[:64],
        "io": "Audio Device",
        "use_sdp": True,
        "source": -1,
        "sdp": sdp_text,
        "delay": 0,
        "ignore_refclk_gmid": True,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.put(
                f"{DAEMON_URL}/api/sink/{sink_id}",
                json=body,
            )
            if r.status_code in (200, 201):
                log.info("aes67-daemon sink %d created: %s", sink_id, name)
                return {"ok": True}
            else:
                msg = f"aes67-daemon returned HTTP {r.status_code}: {r.text[:300]}"
                log.error(msg)
                return {"ok": False, "message": msg}
    except httpx.ConnectError:
        return {"ok": False, "message": f"Connection refused at {DAEMON_URL}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


async def _remove_sink(sink_id: int) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.delete(f"{DAEMON_URL}/api/sink/{sink_id}")
            if r.status_code in (200, 204):
                log.info("aes67-daemon sink %d removed", sink_id)
                return {"ok": True}
            return {"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
