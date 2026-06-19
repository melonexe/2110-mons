"""
SAP (Session Announcement Protocol, RFC 2974) listener.
Discovers AES67 / SMPTE 2110 streams announced on 239.255.255.255:9875.
Parses the embedded SDP to extract stream metadata.
"""

import socket
import struct
import threading
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

log = logging.getLogger(__name__)

SAP_MULTICAST_GROUP = "239.255.255.255"
SAP_PORT = 9875
SAP_TIMEOUT = 1.0
STREAM_EXPIRY_SEC = 30.0


@dataclass
class SAPStream:
    source_ip: str
    session_id: str
    session_name: str
    encoding: str = ""
    sample_rate: int = 0
    channels: int = 0
    payload_type: int = 0
    multicast_addr: str = ""
    dest_port: int = 0
    sdp_raw: str = ""
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("sdp_raw", None)
        d.pop("last_seen", None)
        return d


class SAPListener:
    def __init__(self):
        self._streams: Dict[str, SAPStream] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="sap-listener")
        self._thread.start()
        log.info("SAP listener started on %s:%d", SAP_MULTICAST_GROUP, SAP_PORT)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def get_streams(self) -> list:
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._streams.items()
                       if now - v.last_seen > STREAM_EXPIRY_SEC]
            for k in expired:
                del self._streams[k]
            return [s.to_dict() for s in self._streams.values()]

    # ------------------------------------------------------------------

    def _run(self) -> None:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", SAP_PORT))
            mreq = struct.pack("4sL", socket.inet_aton(SAP_MULTICAST_GROUP),
                               socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(SAP_TIMEOUT)
        except OSError as e:
            log.error("SAP socket error: %s", e)
            return

        while self._running:
            try:
                data, addr = sock.recvfrom(65535)
                self._parse_packet(data, addr[0])
            except socket.timeout:
                self._expire_streams()
            except Exception as e:
                log.debug("SAP recv error: %s", e)

        sock.close()

    def _expire_streams(self) -> None:
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._streams.items()
                       if now - v.last_seen > STREAM_EXPIRY_SEC]
            for k in expired:
                log.info("SAP stream expired: %s", k)
                del self._streams[k]

    def _parse_packet(self, data: bytes, source_ip: str) -> None:
        if len(data) < 4:
            return
        # SAP header: 1 byte flags, 1 byte auth len, 2 bytes msg ID hash
        flags = data[0]
        version = (flags >> 5) & 0x7
        is_ipv6 = (flags >> 4) & 0x1
        deletion = (flags >> 2) & 0x1
        auth_len = data[1]
        msg_id = struct.unpack(">H", data[2:4])[0]

        offset = 4
        # Originating source (4 bytes IPv4 or 16 bytes IPv6)
        if is_ipv6:
            offset += 16
        else:
            offset += 4

        # Skip auth data
        offset += auth_len * 4

        # Optional payload type string
        payload_start = data.find(b"\x00", offset)
        if payload_start == -1:
            return
        content_type = data[offset:payload_start].decode("ascii", errors="ignore").strip()
        offset = payload_start + 1

        sdp_bytes = data[offset:]
        try:
            sdp = sdp_bytes.decode("utf-8", errors="ignore")
        except Exception:
            return

        stream = _parse_sdp(sdp, source_ip)
        if stream is None:
            return

        key = f"{source_ip}:{stream.session_id}"

        with self._lock:
            if deletion and key in self._streams:
                log.info("SAP stream withdrawn: %s", stream.session_name)
                del self._streams[key]
            else:
                if key not in self._streams:
                    log.info("SAP stream discovered: %s from %s", stream.session_name, source_ip)
                self._streams[key] = stream


def _parse_sdp(sdp: str, source_ip: str) -> Optional[SAPStream]:
    lines = sdp.splitlines()
    session_name = ""
    session_id = ""
    encoding = ""
    sample_rate = 0
    channels = 0
    payload_type = 0
    multicast_addr = ""
    dest_port = 0

    for line in lines:
        if line.startswith("s="):
            session_name = line[2:].strip()
        elif line.startswith("o="):
            parts = line[2:].split()
            if len(parts) >= 2:
                session_id = parts[1]
        elif line.startswith("c="):
            # c=IN IP4 239.69.0.1/32
            parts = line[2:].split()
            if len(parts) >= 3:
                addr_part = parts[2].split("/")[0]
                multicast_addr = addr_part
        elif line.startswith("m="):
            # m=audio 5004 RTP/AVP 98
            parts = line[2:].split()
            if len(parts) >= 4 and parts[0] == "audio":
                try:
                    dest_port = int(parts[1])
                    payload_type = int(parts[3])
                except ValueError:
                    pass
        elif line.startswith("a=rtpmap:"):
            # a=rtpmap:98 L24/48000/8
            val = line[9:].strip()
            parts = val.split()
            if len(parts) >= 2:
                codec_info = parts[1].split("/")
                if len(codec_info) >= 1:
                    encoding = codec_info[0]
                if len(codec_info) >= 2:
                    try:
                        sample_rate = int(codec_info[1])
                    except ValueError:
                        pass
                if len(codec_info) >= 3:
                    try:
                        channels = int(codec_info[2])
                    except ValueError:
                        pass

    if not session_name:
        return None

    return SAPStream(
        source_ip=source_ip,
        session_id=session_id,
        session_name=session_name,
        encoding=encoding,
        sample_rate=sample_rate,
        channels=channels,
        payload_type=payload_type,
        multicast_addr=multicast_addr,
        dest_port=dest_port,
        sdp_raw=sdp,
    )
