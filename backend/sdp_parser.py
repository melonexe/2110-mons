"""
SDP parser for AES67 / SMPTE ST 2110-30 session descriptions.
Handles SMPTE 2022-7 redundant streams (a=group:DUP primary secondary).
"""

from dataclasses import dataclass, field, asdict
from typing import List, Optional
import re


@dataclass
class MediaStream:
    mid: str = ""
    label: str = ""
    port: int = 0
    payload_type: int = 0
    multicast_addr: str = ""
    ttl: int = 32
    source_addr: str = ""    # from a=source-filter incl
    encoding: str = ""       # e.g. L24, L16, AM824
    sample_rate: int = 48000
    channels: int = 0
    ptime_ms: float = 1.0
    ptp_domain: int = 0
    ptp_gmid: str = ""       # grandmaster clock ID


@dataclass
class ParsedSDP:
    session_name: str = ""
    origin_addr: str = ""
    session_id: str = ""
    is_redundant: bool = False   # True when a=group:DUP found
    primary: Optional[MediaStream] = None
    secondary: Optional[MediaStream] = None
    all_streams: List[MediaStream] = field(default_factory=list)
    raw: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "session_name": self.session_name,
            "origin_addr": self.origin_addr,
            "session_id": self.session_id,
            "is_redundant": self.is_redundant,
            "primary": asdict(self.primary) if self.primary else None,
            "secondary": asdict(self.secondary) if self.secondary else None,
            "stream_count": len(self.all_streams),
            "error": self.error,
        }


def parse_sdp(text: str) -> ParsedSDP:
    result = ParsedSDP(raw=text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Session-level fields ───────────────────────────────────────
    dup_mids: List[str] = []   # ordered [primary_mid, secondary_mid]

    for line in lines:
        if line.startswith("o="):
            # o=<username> <sess-id> <sess-version> <nettype> <addrtype> <addr>
            parts = line[2:].split()
            if len(parts) >= 2:
                result.session_id = parts[1]
            if len(parts) >= 6:
                result.origin_addr = parts[5]

        elif line.startswith("s="):
            result.session_name = line[2:].strip()

        elif line.startswith("a=group:DUP"):
            # a=group:DUP primary secondary
            mids = line.split()[1:]  # skip "a=group:DUP"
            dup_mids = mids
            result.is_redundant = True

    # ── Split into m= sections ─────────────────────────────────────
    sections: List[List[str]] = []
    current: List[str] = []
    in_media = False

    for line in lines:
        if line.startswith("m="):
            if in_media:
                sections.append(current)
            current = [line]
            in_media = True
        elif in_media:
            current.append(line)
        # session-level lines before first m= are ignored per-stream

    if current and in_media:
        sections.append(current)

    # ── Parse each m= section ──────────────────────────────────────
    for sec in sections:
        stream = _parse_media_section(sec)
        result.all_streams.append(stream)

    # ── Map to primary / secondary via DUP group ───────────────────
    if result.is_redundant and dup_mids:
        mid_map = {s.mid: s for s in result.all_streams if s.mid}
        if len(dup_mids) >= 1:
            result.primary = mid_map.get(dup_mids[0])
        if len(dup_mids) >= 2:
            result.secondary = mid_map.get(dup_mids[1])
    elif result.all_streams:
        result.primary = result.all_streams[0]
        if len(result.all_streams) > 1:
            result.secondary = result.all_streams[1]

    if not result.primary and result.all_streams:
        result.primary = result.all_streams[0]

    return result


def _parse_media_section(lines: List[str]) -> MediaStream:
    s = MediaStream()

    for line in lines:
        # m=audio <port> RTP/AVP <pt>
        if line.startswith("m=audio"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    s.port = int(parts[1])
                except ValueError:
                    pass
            if len(parts) >= 4:
                try:
                    s.payload_type = int(parts[3])
                except ValueError:
                    pass

        # i=<label>
        elif line.startswith("i="):
            s.label = line[2:].strip()

        # c=IN IP4 239.x.x.x/TTL  (TTL, not CIDR prefix)
        elif line.startswith("c="):
            parts = line[2:].split()
            if len(parts) >= 3:
                addr_part = parts[2]
                if "/" in addr_part:
                    addr, ttl = addr_part.split("/", 1)
                    s.multicast_addr = addr
                    try:
                        s.ttl = int(ttl.split("/")[0])
                    except ValueError:
                        pass
                else:
                    s.multicast_addr = addr_part

        # a=rtpmap:PT encoding/rate/channels
        elif line.startswith("a=rtpmap:"):
            val = line[9:].strip()
            parts = val.split(None, 1)
            if len(parts) == 2:
                codec_parts = parts[1].split("/")
                if codec_parts:
                    s.encoding = codec_parts[0]
                if len(codec_parts) >= 2:
                    try:
                        s.sample_rate = int(codec_parts[1])
                    except ValueError:
                        pass
                if len(codec_parts) >= 3:
                    try:
                        s.channels = int(codec_parts[2])
                    except ValueError:
                        pass

        # a=ptime:<ms>
        elif line.startswith("a=ptime:"):
            try:
                s.ptime_ms = float(line[8:].strip())
            except ValueError:
                pass

        # a=mid:<id>
        elif line.startswith("a=mid:"):
            s.mid = line[6:].strip()

        # a=source-filter: incl IN IP4 <mcast> <src>
        elif line.startswith("a=source-filter:"):
            m = re.search(r'incl\s+IN\s+IP4\s+\S+\s+(\S+)', line)
            if m:
                s.source_addr = m.group(1)

        # a=ts-refclk:ptp=IEEE1588-2008:<GMID>:<domain>
        elif line.startswith("a=ts-refclk:ptp="):
            m = re.search(r'IEEE1588-2008:([^:]+):(\d+)', line)
            if m:
                s.ptp_gmid = m.group(1)
                try:
                    s.ptp_domain = int(m.group(2))
                except ValueError:
                    pass

        # a=clock-domain:PTPv2 <domain>
        elif line.startswith("a=clock-domain:PTPv2"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    s.ptp_domain = int(parts[-1])
                except ValueError:
                    pass

    return s
