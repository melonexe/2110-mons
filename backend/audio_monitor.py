"""
Audio monitor: reads from an ALSA device or an AES67 RTP stream, runs the
metering pipeline, and exposes snapshots for the WebSocket push.

Two start modes:
  start(config)          — open an ALSA PCM capture device
  start_from_rtp(parsed) — receive AES67 RTP directly (no driver needed)
"""

import alsaaudio
import numpy as np
import threading
import time
import logging
from dataclasses import dataclass, asdict
from typing import Callable, List, Optional

from meters import ChannelMeter, compute_phase

log = logging.getLogger(__name__)

PERIOD_FRAMES  = 1024
FORMAT         = alsaaudio.PCM_FORMAT_S32_LE
INT32_MAX      = 2 ** 31


@dataclass
class MonitorConfig:
    device: str = "default"
    num_channels: int = 2
    sample_rate: int = 48000
    period_frames: int = PERIOD_FRAMES


class AudioMonitor:
    def __init__(self):
        self._config   = MonitorConfig()
        self._mode     = "idle"         # "alsa" | "rtp" | "idle"
        self._running  = False
        self._thread:  Optional[threading.Thread] = None
        self._rtp:     Optional[object]           = None   # RTPReceiver instance
        self._lock     = threading.Lock()
        self._meters:  List[ChannelMeter] = []
        self._snapshots:   List[dict] = []
        self._phase_pairs: List[dict] = []
        self._error:   Optional[str]  = None
        self._audio_callback: Optional[Callable] = None
        self._int_counter  = 0
        self._int_interval = 5          # process_block calls between integrated updates

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, config: MonitorConfig) -> None:
        """Open an ALSA capture device and start metering."""
        self.stop()
        with self._lock:
            self._config  = config
            self._error   = None
            self._mode    = "alsa"
            self._init_meters(config.num_channels, config.sample_rate)
        self._running = True
        self._thread  = threading.Thread(target=self._run_alsa, daemon=True,
                                         name="alsa-reader")
        self._thread.start()

    def start_from_rtp(self, parsed) -> None:
        """Subscribe to an AES67 stream via direct RTP reception."""
        from rtp_receiver import RTPReceiver
        self.stop()

        stream = parsed.primary
        cfg = MonitorConfig(
            device="rtp",
            num_channels=stream.channels or 2,
            sample_rate=stream.sample_rate or 48000,
        )
        with self._lock:
            self._config = cfg
            self._error  = None
            self._mode   = "rtp"
            self._init_meters(cfg.num_channels, cfg.sample_rate)

        self._rtp = RTPReceiver()
        self._rtp.set_audio_callback(self._process_block)
        self._running = True
        self._rtp.start(parsed)

    def stop(self) -> None:
        self._running = False
        if self._rtp:
            self._rtp.stop()
            self._rtp = None
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._mode = "idle"

    def get_state(self) -> dict:
        with self._lock:
            return {
                "channels":    list(self._snapshots),
                "phase_pairs": list(self._phase_pairs),
                "error":       self._error or (self._rtp.get_error() if self._rtp else None),
                "config":      asdict(self._config),
                "mode":        self._mode,
            }

    def reset_clip(self) -> None:
        with self._lock:
            for m in self._meters:
                m.reset_clip()

    def reset_integrated(self) -> None:
        with self._lock:
            for m in self._meters:
                m.reset_integrated()

    def set_audio_callback(self, fn: Optional[Callable]) -> None:
        self._audio_callback = fn

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _init_meters(self, n_ch: int, sample_rate: int) -> None:
        """Initialise meter objects — call while holding self._lock."""
        self._meters      = [ChannelMeter(sample_rate=sample_rate)
                             for _ in range(n_ch)]
        self._snapshots   = []
        self._phase_pairs = []
        self._int_counter = 0
        self._int_interval = max(1, int(sample_rate * 0.1 / PERIOD_FRAMES))

    def _process_block(self, samples_f: np.ndarray) -> None:
        """
        Central audio processing callback — called from either the ALSA thread
        or the RTP receiver thread.

        samples_f: float32 array of shape (frames, channels), range −1..+1
        """
        # Deliver to audio streamer (browser listen) before acquiring lock
        if self._audio_callback:
            try:
                self._audio_callback(samples_f)
            except Exception:
                pass

        n_ch   = samples_f.shape[1] if samples_f.ndim > 1 else 1
        frames = len(samples_f)

        with self._lock:
            for ch in range(min(n_ch, len(self._meters))):
                self._meters[ch].process(samples_f[:, ch], frames)

            self._int_counter += 1
            if self._int_counter >= self._int_interval:
                self._int_counter = 0
                for m in self._meters:
                    m.update_integrated()

            self._snapshots = [asdict(m.snapshot()) for m in self._meters]

            pairs = []
            for i in range(0, min(n_ch, len(self._meters)) - 1, 2):
                pairs.append({
                    "left":  i,
                    "right": i + 1,
                    "value": round(compute_phase(samples_f[:, i],
                                                 samples_f[:, i + 1]), 3),
                })
            self._phase_pairs = pairs

    # ── ALSA reader thread ────────────────────────────────────────────────────

    def _run_alsa(self) -> None:
        cfg = self._config
        try:
            pcm = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                mode=alsaaudio.PCM_NORMAL,
                device=cfg.device,
                channels=cfg.num_channels,
                rate=cfg.sample_rate,
                format=FORMAT,
                periodsize=cfg.period_frames,
            )
            log.info("Opened ALSA device %s (%d ch @ %d Hz)",
                     cfg.device, cfg.num_channels, cfg.sample_rate)
        except alsaaudio.ALSAAudioError as e:
            with self._lock:
                self._error = f"Cannot open ALSA device '{cfg.device}': {e}"
            log.error(self._error)
            return

        while self._running:
            try:
                length, raw = pcm.read()
            except alsaaudio.ALSAAudioError as e:
                with self._lock:
                    self._error = str(e)
                log.error("ALSA read error: %s", e)
                time.sleep(0.1)
                continue

            if length <= 0:
                continue

            samples_int = np.frombuffer(raw, dtype=np.int32)
            expected    = length * cfg.num_channels
            if len(samples_int) < expected:
                continue

            samples_f = (samples_int[:expected]
                         .reshape(length, cfg.num_channels)
                         .astype(np.float32) / INT32_MAX)

            self._process_block(samples_f)

        pcm.close()


def list_alsa_devices() -> List[dict]:
    """Return capture-capable ALSA card/device entries."""
    devices = []
    try:
        cards = alsaaudio.cards()
        pcms  = alsaaudio.pcms(alsaaudio.PCM_CAPTURE)
        for idx, name in enumerate(cards):
            for pcm_name in pcms:
                if f"hw:{idx}" in pcm_name or name.lower() in pcm_name.lower():
                    devices.append({
                        "card_index":   idx,
                        "card_name":    name,
                        "device_string": pcm_name,
                    })
    except Exception as e:
        log.warning("Could not enumerate ALSA devices: %s", e)

    if not devices:
        try:
            for p in alsaaudio.pcms(alsaaudio.PCM_CAPTURE):
                devices.append({"card_index": -1, "card_name": p, "device_string": p})
        except Exception:
            pass

    return devices
