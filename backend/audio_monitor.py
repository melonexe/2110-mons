"""
Reads audio from an ALSA PCM capture device and drives the meter pipeline.
Runs in a background thread; exposes meter snapshots via a shared dict.
"""

import alsaaudio
import numpy as np
import threading
import time
import logging
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List, Optional
from meters import ChannelMeter, ChannelMeterState, compute_phase

log = logging.getLogger(__name__)

PERIOD_FRAMES = 1024
FORMAT = alsaaudio.PCM_FORMAT_S32_LE
BYTES_PER_SAMPLE = 4
INT32_MAX = 2 ** 31


@dataclass
class MonitorConfig:
    device: str = "default"
    num_channels: int = 2
    sample_rate: int = 48000
    period_frames: int = PERIOD_FRAMES


class AudioMonitor:
    def __init__(self):
        self._config = MonitorConfig()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._meters: List[ChannelMeter] = []
        self._snapshots: List[dict] = []
        self._phase_pairs: List[dict] = []  # [{left, right, value}]
        self._error: Optional[str] = None
        self._device_info: dict = {}
        self._audio_callback: Optional[Callable] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, config: MonitorConfig) -> None:
        self.stop()
        with self._lock:
            self._config = config
            self._meters = [ChannelMeter(sample_rate=config.sample_rate)
                            for _ in range(config.num_channels)]
            self._snapshots = []
            self._phase_pairs = []
            self._error = None
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def get_state(self) -> dict:
        with self._lock:
            return {
                "channels": list(self._snapshots),
                "phase_pairs": list(self._phase_pairs),
                "error": self._error,
                "config": asdict(self._config),
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
        """Register a callback invoked for each audio chunk (capture thread).
        fn(samples_f: np.ndarray) where samples_f is shape (frames, channels) float32.
        """
        self._audio_callback = fn

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        cfg = self._config
        pcm = None
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

        integrated_counter = 0
        # Update integrated every ~100 ms
        integrated_interval = max(1, int(cfg.sample_rate * 0.1 / cfg.period_frames))

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

            # Deinterleave: raw bytes -> (channels, frames) float32
            samples_int = np.frombuffer(raw, dtype=np.int32)
            expected = length * cfg.num_channels
            if len(samples_int) < expected:
                continue
            samples_int = samples_int[:expected]
            samples_f = samples_int.reshape(length, cfg.num_channels).astype(np.float32)
            samples_f /= INT32_MAX

            # Deliver raw audio to any registered callback (e.g. audio streamer)
            if self._audio_callback:
                try:
                    self._audio_callback(samples_f)
                except Exception:
                    pass

            with self._lock:
                for ch, meter in enumerate(self._meters):
                    meter.process(samples_f[:, ch], length)

                integrated_counter += 1
                if integrated_counter >= integrated_interval:
                    integrated_counter = 0
                    for meter in self._meters:
                        meter.update_integrated()

                snaps = []
                for ch, meter in enumerate(self._meters):
                    s = meter.snapshot()
                    snaps.append(asdict(s))
                self._snapshots = snaps

                # Phase for consecutive stereo pairs
                pairs = []
                for i in range(0, cfg.num_channels - 1, 2):
                    left = samples_f[:, i]
                    right = samples_f[:, i + 1]
                    pairs.append({
                        "left": i,
                        "right": i + 1,
                        "value": round(compute_phase(left, right), 3),
                    })
                self._phase_pairs = pairs

        if pcm:
            pcm.close()


def list_alsa_devices() -> List[dict]:
    """Return capture-capable ALSA card/device entries."""
    devices = []
    try:
        cards = alsaaudio.cards()
        for idx, name in enumerate(cards):
            # Check for capture-capable PCMs
            try:
                pcms = alsaaudio.pcms(alsaaudio.PCM_CAPTURE)
                for pcm_name in pcms:
                    if f"hw:{idx}" in pcm_name or name.lower() in pcm_name.lower():
                        devices.append({
                            "card_index": idx,
                            "card_name": name,
                            "device_string": pcm_name,
                        })
            except Exception:
                pass
    except Exception as e:
        log.warning("Could not enumerate ALSA devices: %s", e)

    if not devices:
        # Fallback: return all capture PCMs
        try:
            pcms = alsaaudio.pcms(alsaaudio.PCM_CAPTURE)
            for p in pcms:
                devices.append({"card_index": -1, "card_name": p, "device_string": p})
        except Exception:
            pass

    return devices
