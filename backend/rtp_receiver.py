"""
Direct AES67 / SMPTE ST 2110-30 RTP receiver.

No driver, no daemon, no ALSA loopback required.
Joins the multicast group described in the SDP, receives RTP/UDP packets,
decodes L24 / L16 / AM824 audio to float32, and delivers blocks to a callback.

Supported encodings: L24, L16, AM824 (AES3-over-RTP)
Supported multicast: ASM (IP_ADD_MEMBERSHIP) and SSM (IP_ADD_SOURCE_MEMBERSHIP)
"""

import logging
import socket
import struct
import threading
import numpy as np
from typing import Callable, Optional

from sdp_parser import MediaStream, ParsedSDP

log = logging.getLogger(__name__)

RTP_HEADER_BYTES = 12
RECV_BUF_BYTES   = 4 * 1024 * 1024   # 4 MB socket receive buffer
BLOCK_FRAMES     = 1024               # accumulate this many frames before callback


class RTPReceiver:
    """
    Receives one AES67 RTP stream (primary leg of a DUP pair) and delivers
    float32 audio blocks to the registered callback.

    Thread model: one background daemon thread does blocking recvfrom().
    The callback is invoked from that thread — keep it fast and non-blocking.
    """

    def __init__(self):
        self._stream: Optional[MediaStream] = None
        self._running  = False
        self._thread:  Optional[threading.Thread] = None
        self._sock:    Optional[socket.socket]    = None
        self._audio_cb: Optional[Callable]        = None
        self._error:   Optional[str]              = None

    # ── Public API ────────────────────────────────────────────────────────────

    def set_audio_callback(self, fn: Optional[Callable]) -> None:
        """fn(samples_f: np.ndarray)  shape (frames, channels), float32 −1..+1"""
        self._audio_cb = fn

    def start(self, parsed: ParsedSDP) -> None:
        self.stop()
        stream = parsed.primary
        if stream is None:
            self._error = "No primary audio stream in SDP."
            return
        if not stream.multicast_addr:
            self._error = "SDP has no connection address (c= line)."
            return
        if stream.channels == 0:
            self._error = "SDP has no channel count (a=rtpmap missing?)."
            return

        self._stream  = stream
        self._error   = None
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True,
                                         name=f"rtp-rx-{stream.multicast_addr}")
        self._thread.start()
        log.info("RTP receiver starting: %s:%d  %s %d ch @ %d Hz",
                 stream.multicast_addr, stream.port,
                 stream.encoding, stream.channels, stream.sample_rate)

    def stop(self) -> None:
        self._running = False
        # Close the socket to unblock recvfrom()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def get_error(self) -> Optional[str]:
        return self._error

    def is_running(self) -> bool:
        return self._running and (self._thread is not None) and self._thread.is_alive()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        stream = self._stream
        n_ch   = stream.channels
        enc    = stream.encoding.upper().split("/")[0]  # strip trailing /rate/ch if present

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError:
                pass
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUF_BYTES)
            sock.bind(("", stream.port))
            sock.settimeout(2.0)
            self._sock = sock

            _join_multicast(sock, stream.multicast_addr, stream.source_addr)

        except OSError as e:
            self._error = f"Socket setup failed: {e}"
            log.error(self._error)
            self._running = False
            return

        # Accumulation buffer — collect small RTP payloads into larger blocks
        accum = np.empty((0, n_ch), dtype=np.float32)

        while self._running:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            payload = _strip_rtp_header(data)
            if payload is None:
                continue

            try:
                block = _decode_payload(payload, enc, n_ch)
            except Exception as e:
                log.debug("Decode error: %s", e)
                continue

            accum = np.concatenate((accum, block), axis=0)

            while len(accum) >= BLOCK_FRAMES:
                chunk    = accum[:BLOCK_FRAMES]
                accum    = accum[BLOCK_FRAMES:]
                if self._audio_cb:
                    try:
                        self._audio_cb(chunk)
                    except Exception:
                        pass

        try:
            sock.close()
        except OSError:
            pass
        self._running = False
        log.info("RTP receiver stopped.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _join_multicast(sock: socket.socket, mcast_addr: str, source_addr: str) -> None:
    if source_addr:
        # Source-Specific Multicast (SSM) — RFC 4607
        # IP_ADD_SOURCE_MEMBERSHIP: struct ip_mreq_source { mcast, iface, source }
        mreq = (socket.inet_aton(mcast_addr) +
                socket.inet_aton("0.0.0.0") +
                socket.inet_aton(source_addr))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_SOURCE_MEMBERSHIP, mreq)
        log.info("SSM join: group=%s source=%s", mcast_addr, source_addr)
    else:
        mreq = struct.pack("4sL", socket.inet_aton(mcast_addr), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        log.info("ASM join: group=%s", mcast_addr)


def _strip_rtp_header(data: bytes) -> Optional[bytes]:
    """Return the RTP payload, or None if the packet is malformed."""
    if len(data) < RTP_HEADER_BYTES:
        return None
    flags    = data[0]
    cc       = flags & 0x0F          # CSRC count
    has_ext  = (flags >> 4) & 0x01
    offset   = RTP_HEADER_BYTES + cc * 4

    if has_ext:
        if len(data) < offset + 4:
            return None
        ext_words = struct.unpack_from("!H", data, offset + 2)[0]
        offset   += 4 + ext_words * 4

    return data[offset:] if offset < len(data) else None


def _decode_payload(payload: bytes, encoding: str, n_ch: int) -> np.ndarray:
    """Convert raw RTP payload bytes to float32 array of shape (frames, n_ch)."""
    if encoding == "L24":
        return _l24(payload, n_ch)
    elif encoding == "L16":
        return _l16(payload, n_ch)
    elif encoding == "AM824":
        return _am824(payload, n_ch)
    else:
        raise ValueError(f"Unsupported encoding: {encoding!r}")


def _l24(payload: bytes, n_ch: int) -> np.ndarray:
    """24-bit signed big-endian PCM → float32"""
    raw = np.frombuffer(payload, dtype=np.uint8)
    n   = (len(raw) // 3) * 3          # trim to complete samples
    raw = raw[:n]
    n_samples = n // 3

    # Reconstruct sign-extended int32 from 3 big-endian bytes.
    # Place bytes in top 3 bytes of int32, then arithmetic-shift right 8.
    s32 = ((raw[0::3].astype(np.int32) << 24) |
           (raw[1::3].astype(np.int32) << 16) |
           (raw[2::3].astype(np.int32) <<  8))
    s32 >>= 8   # arithmetic right-shift preserves sign bit

    f = s32.astype(np.float32) / 8_388_608.0   # 2^23
    n_frames = n_samples // n_ch
    return f[:n_frames * n_ch].reshape(n_frames, n_ch)


def _l16(payload: bytes, n_ch: int) -> np.ndarray:
    """16-bit signed big-endian PCM → float32"""
    s16 = np.frombuffer(payload, dtype=">i2")
    f   = s16.astype(np.float32) / 32_768.0
    n_frames = len(f) // n_ch
    return f[:n_frames * n_ch].reshape(n_frames, n_ch)


def _am824(payload: bytes, n_ch: int) -> np.ndarray:
    """AES3-over-RTP (AM824): 32-bit words, top 8 bits are validity/user bits."""
    s32 = np.frombuffer(payload, dtype=">i4")
    # Audio in bits 23..0 of each word; shift into int24 range
    audio = (s32 << 8) >> 8
    f     = audio.astype(np.float32) / 8_388_608.0
    n_frames = len(f) // n_ch
    return f[:n_frames * n_ch].reshape(n_frames, n_ch)
