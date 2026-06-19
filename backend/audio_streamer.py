"""
Bridges the audio capture thread to async WebSocket clients.

The capture thread calls feed() with each raw audio block.
An asyncio background task (run_forever) drains the queue and
sends binary float32 frames to all connected browsers.

Wire-format per message: interleaved float32 LE stereo
  [L0, R0, L1, R1, ..., L(N-1), R(N-1)]
"""

import asyncio
import queue
import logging
import numpy as np
from typing import Set, Tuple

from fastapi import WebSocket

log = logging.getLogger(__name__)


class AudioStreamer:
    def __init__(self):
        # Thread-safe queue written from capture thread, read by asyncio task
        self._q: queue.Queue = queue.Queue(maxsize=100)
        self._clients: Set[WebSocket] = set()
        self._pair: Tuple[int, int] = (0, 1)  # left/right channel indices

    # ── Called from the audio monitor's capture thread ────────────────────────

    def feed(self, samples_f: np.ndarray) -> None:
        """
        samples_f: float32 array of shape (frames, channels), range -1..+1.
        Extracts the selected stereo pair and enqueues for WebSocket delivery.
        """
        if not self._clients:
            return

        n_ch = samples_f.shape[1] if samples_f.ndim > 1 else 1
        l = min(self._pair[0], n_ch - 1)
        r = min(self._pair[1], n_ch - 1)

        if n_ch == 1:
            left  = samples_f[:, 0]
            right = samples_f[:, 0]
        else:
            left  = samples_f[:, l]
            right = samples_f[:, r]

        # Interleave L/R and convert to raw bytes
        stereo = np.empty(len(left) * 2, dtype=np.float32)
        stereo[0::2] = left
        stereo[1::2] = right

        try:
            self._q.put_nowait(stereo.tobytes())
        except queue.Full:
            pass  # drop the frame rather than block the capture thread

    # ── WebSocket client management ───────────────────────────────────────────

    def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        log.info("Audio client connected (%d total)", len(self._clients))

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        log.info("Audio client disconnected (%d total)", len(self._clients))

    def set_monitor_pair(self, left: int, right: int) -> None:
        self._pair = (max(0, left), max(0, right))
        log.debug("Audio monitor pair: %d + %d", left, right)

    # ── Asyncio background task ───────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Drain the queue and fan-out binary frames to all WebSocket clients."""
        while True:
            try:
                data = self._q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue

            if not self._clients:
                continue

            dead: Set[WebSocket] = set()
            for ws in list(self._clients):
                try:
                    await ws.send_bytes(data)
                except Exception:
                    dead.add(ws)

            for ws in dead:
                self.remove_client(ws)
