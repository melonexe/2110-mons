"""
Audio metering: peak/PPM, RMS, EBU R128 loudness, phase correlation.
All functions operate on numpy float32 arrays normalised to [-1.0, 1.0].
"""

import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import List

SAMPLE_RATE = 48000

# EBU R128 K-weighting filter coefficients (48 kHz)
# Stage 1: pre-filter (high-shelf)
_B1 = np.array([1.53512485958697, -2.69169618940638, 1.19839281085285])
_A1 = np.array([1.0, -1.69065929318241, 0.73248077421585])
# Stage 2: RLB weighting (high-pass)
_B2 = np.array([1.0, -2.0, 1.0])
_A2 = np.array([1.0, -1.99004745483398, 0.99007225036621])


def _k_weight(samples: np.ndarray) -> np.ndarray:
    """Apply K-weighting filter to a mono signal."""
    from scipy.signal import lfilter
    x = lfilter(_B1, _A1, samples)
    x = lfilter(_B2, _A2, x)
    return x


@dataclass
class ChannelMeterState:
    peak_db: float = -100.0
    peak_hold_db: float = -100.0
    rms_db: float = -100.0
    loudness_m: float = -100.0   # EBU R128 momentary (400 ms)
    loudness_st: float = -100.0  # EBU R128 short-term (3 s)
    loudness_i: float = -100.0   # EBU R128 integrated
    phase: float = 0.0           # correlation coefficient -1..1 (stereo pairs only)
    clip: bool = False


@dataclass
class ChannelMeter:
    """Stateful per-channel meter accumulating audio blocks."""
    sample_rate: int = SAMPLE_RATE
    # Peak hold: seconds before decay starts
    peak_hold_time: float = 2.0
    # PPM decay: dB per second (Type I broadcast PPM)
    peak_decay_rate: float = 20.0

    _peak_hold_db: float = field(default=-100.0, init=False, repr=False)
    _peak_hold_samples: int = field(default=0, init=False, repr=False)
    _peak_db: float = field(default=-100.0, init=False, repr=False)

    # EBU R128 block buffers (overlap-add style)
    _momentary_buf: deque = field(default_factory=lambda: deque(maxlen=int(SAMPLE_RATE * 0.4)),
                                   init=False, repr=False)
    _short_buf: deque = field(default_factory=lambda: deque(maxlen=int(SAMPLE_RATE * 3.0)),
                               init=False, repr=False)
    # Integrated loudness gating accumulators
    _int_sum: float = field(default=0.0, init=False, repr=False)
    _int_count: int = field(default=0, init=False, repr=False)
    _int_gated_sum: float = field(default=0.0, init=False, repr=False)
    _int_gated_count: int = field(default=0, init=False, repr=False)
    _int_db: float = field(default=-100.0, init=False, repr=False)

    _clip: bool = field(default=False, init=False, repr=False)
    _rms_db: float = field(default=-100.0, init=False, repr=False)

    def process(self, block: np.ndarray, block_samples: int) -> None:
        """Update meter state with a new block of mono float32 samples."""
        if len(block) == 0:
            return

        # Peak
        peak_linear = np.max(np.abs(block))
        if peak_linear >= 1.0:
            self._clip = True
        peak_db = _to_db(peak_linear)
        if peak_db > self._peak_db:
            self._peak_db = peak_db
        else:
            # PPM decay
            decay = self.peak_decay_rate * block_samples / self.sample_rate
            self._peak_db = max(self._peak_db - decay, peak_db)
            self._peak_db = max(self._peak_db, -100.0)

        # Peak hold
        if peak_db >= self._peak_hold_db:
            self._peak_hold_db = peak_db
            self._peak_hold_samples = int(self.peak_hold_time * self.sample_rate)
        else:
            if self._peak_hold_samples > 0:
                self._peak_hold_samples -= block_samples
            else:
                decay = self.peak_decay_rate * block_samples / self.sample_rate
                self._peak_hold_db = max(self._peak_hold_db - decay, -100.0)

        # RMS
        rms = np.sqrt(np.mean(block ** 2))
        self._rms_db = _to_db(rms)

        # K-weighted buffer for EBU R128
        kw = _k_weight(block)
        self._momentary_buf.extend(kw)
        self._short_buf.extend(kw)

    def get_loudness_momentary(self) -> float:
        return _mean_square_db(np.array(self._momentary_buf))

    def get_loudness_short_term(self) -> float:
        return _mean_square_db(np.array(self._short_buf))

    def update_integrated(self) -> None:
        """Call once per 100 ms gating block to advance integrated loudness."""
        if len(self._momentary_buf) < int(self.sample_rate * 0.4):
            return
        block = np.array(self._momentary_buf)
        ms = float(np.mean(block ** 2))
        self._int_sum += ms
        self._int_count += 1
        if ms > 0 and 10 * np.log10(ms) > -70.0:
            self._int_gated_sum += ms
            self._int_gated_count += 1

        if self._int_gated_count > 0:
            mean_ms = self._int_gated_sum / self._int_gated_count
            if mean_ms > 0:
                self._int_db = -0.691 + 10 * np.log10(mean_ms)

    def snapshot(self) -> ChannelMeterState:
        return ChannelMeterState(
            peak_db=round(self._peak_db, 1),
            peak_hold_db=round(self._peak_hold_db, 1),
            rms_db=round(self._rms_db, 1),
            loudness_m=round(self.get_loudness_momentary(), 1),
            loudness_st=round(self.get_loudness_short_term(), 1),
            loudness_i=round(self._int_db, 1),
            clip=self._clip,
        )

    def reset_clip(self) -> None:
        self._clip = False

    def reset_integrated(self) -> None:
        self._int_sum = 0.0
        self._int_count = 0
        self._int_gated_sum = 0.0
        self._int_gated_count = 0
        self._int_db = -100.0


def compute_phase(left: np.ndarray, right: np.ndarray) -> float:
    """Pearson correlation between two channels as a phase/correlation meter."""
    if len(left) == 0 or len(right) == 0:
        return 0.0
    denom = np.sqrt(np.sum(left ** 2) * np.sum(right ** 2))
    if denom < 1e-10:
        return 0.0
    return float(np.clip(np.sum(left * right) / denom, -1.0, 1.0))


def _to_db(linear: float) -> float:
    if linear < 1e-10:
        return -100.0
    return max(20.0 * np.log10(linear), -100.0)


def _mean_square_db(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return -100.0
    ms = float(np.mean(samples ** 2))
    if ms < 1e-10:
        return -100.0
    return max(-0.691 + 10.0 * np.log10(ms), -100.0)
