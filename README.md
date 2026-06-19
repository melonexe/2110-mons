# AES67 / SMPTE ST 2110 Audio Monitor

Browser-based audio monitoring for AES67 and SMPTE ST 2110-30 streams.
Runs on a Linux VM (VirtualBox) accessible from your Windows PC.

---

## Architecture

```
Windows PC
└── VirtualBox VM — Ubuntu 22.04, bridged NIC
    │
    ├── ptp4l              IEEE 1588 slave (software timestamps)
    │
    ├── aes67-linux-daemon  receives AES67 RTP streams
    │     ├── REST API on :9090  ← our backend subscribes here
    │     └── writes audio → snd-aloop (virtual ALSA loopback)
    │
    ├── snd-aloop          kernel module — virtual ALSA loopback
    │     ├── hw:Loopback,0  ← daemon writes here
    │     └── hw:Loopback,1  ← our monitor reads from here
    │
    └── aes67-monitor      our Python app on :8080
          ├── reads hw:Loopback,1 via python-alsaaudio
          ├── computes PPM / EBU R128 / phase
          ├── streams audio to browser via WebSocket
          └── serves the web UI
```

Browse to `http://<vm-ip>:8080` from Windows.

---

## VirtualBox prerequisites

| Setting | Value |
|---|---|
| Guest OS | Ubuntu 22.04 LTS (Server or Desktop) |
| RAM | ≥ 2 GB |
| CPUs | ≥ 2 |
| **Network adapter 1** | **Bridged — attached to the NIC carrying AES67 traffic** |

> Bridged networking is mandatory. The VM must receive multicast RTP and SAP
> packets directly. NAT mode will not work.

---

## Installation

### Step 1 — Copy the project to the VM

```bash
# From Windows PowerShell, if the VM is reachable via SSH:
scp -r "C:\path\to\2110-mons" user@<vm-ip>:~/2110-mons

# Or use a VirtualBox Shared Folder mounted on the VM.
```

### Step 2 — Run the system setup script

Installs all apt packages, loads `snd-aloop`, tunes network buffers,
configures `ptp4l`, creates the Python virtualenv, installs the monitor
as a systemd service.

```bash
cd ~/2110-mons
chmod +x setup/vm-setup.sh
./setup/vm-setup.sh
```

**What gets installed:**

| Package | Why |
|---|---|
| `build-essential cmake g++` | Build aes67-linux-daemon from source |
| `python3-dev` | Compile python-alsaaudio C extension |
| `libasound2-dev` | ALSA headers — required by python-alsaaudio at build time |
| `libboost-dev libboost-system-dev` | Boost.ASIO networking in aes67-daemon |
| `libavahi-client-dev libavahi-common-dev` | mDNS/DNS-SD for stream discovery |
| `libsamplerate0-dev` | Sample rate conversion in aes67-daemon |
| `linuxptp` | Provides `ptp4l` and `phc2sys` |
| `snd-aloop` (kernel module) | Virtual ALSA loopback — the bridge between daemon and our app |

**Python packages** (installed into `.venv/`):

| Package | Why |
|---|---|
| `fastapi` + `uvicorn` | Web framework and ASGI server |
| `python-alsaaudio` | Read from ALSA loopback capture device |
| `numpy` | Audio buffer processing |
| `scipy` | K-weighting filter for EBU R128 |
| `pyloudnorm` | EBU R128 loudness metering |
| `httpx` | Async HTTP client — calls the aes67-daemon REST API |
| `websockets` | Real-time meter and audio data to browser |

### Step 3 — Build and install aes67-linux-daemon

```bash
chmod +x setup/install-aes67-daemon.sh
./setup/install-aes67-daemon.sh
```

This script:
1. Clones `https://github.com/bondagit/aes67-linux-daemon`
2. Builds it with cmake
3. Installs the binary to `/usr/local/bin/aes67-daemon`
4. Writes `/etc/aes67-daemon/daemon.conf` with your detected IP and interface
5. Adds `/etc/asound.conf` so the ALSA alias `aes67` points to `hw:Loopback,1`
6. Installs and starts an `aes67-daemon` systemd service

> **Review `/etc/aes67-daemon/daemon.conf` after installation.**
> Check `ptp_domain` matches your network (commonly `0` or `127`).
> Check `ip_addr` is the correct IP on the AES67 network.

### Step 4 — Verify everything is running

```bash
# All three services should be active
systemctl status ptp4l aes67-daemon aes67-monitor

# PTP should eventually show SLAVE and offset shrinking
journalctl -fu ptp4l

# aes67-daemon REST API should respond
curl http://localhost:9090/api/config

# ALSA loopback devices should be visible
aplay -l   # look for "Loopback" card
arecord -l
```

### Step 5 — Open the monitor

Browse to `http://<vm-ip>:8080` from your Windows browser.

---

## Using the monitor

### Subscribing to a stream via SDP

1. Click **+ SDP Subscribe** in the header
2. Paste the SDP (ST 2110-30 or AES67 format)
3. Click **Parse SDP** — the UI shows stream details (multicast group, channels, PTP clock)
4. Click **Subscribe & Monitor** — the backend POSTs the SDP to the aes67-daemon via its REST API
5. The daemon joins the multicast group and receives the stream
6. Audio flows: AES67 RTP → aes67-daemon → `hw:Loopback,0` → `hw:Loopback,1` → our monitor
7. The device selector auto-fills with `hw:Loopback,1` and monitoring starts

### Monitoring a stream already subscribed in the daemon

If the aes67-daemon already has an active sink (e.g. from a previous session or the daemon's own web UI):

1. Select device `aes67` or `hw:Loopback,1` from the Device dropdown
2. Set Channels and Rate to match the stream
3. Click **▶ Start**

### Listening in the browser

Click **▶ Listen** in the side panel. The browser opens a WebSocket to `/ws/audio`,
receives float32 stereo PCM at 48 kHz, and plays it through the Web Audio API
using an AudioWorklet with an 85 ms jitter buffer.

Select which stereo pair to monitor using the **Pair** dropdown (CH 1+2, 3+4, etc.).

---

## Service management

```bash
# Restart everything
sudo systemctl restart ptp4l aes67-daemon aes67-monitor

# Live logs
journalctl -fu aes67-monitor    # our app
journalctl -fu aes67-daemon     # AES67 stream receiver
journalctl -fu ptp4l            # IEEE 1588 PTP sync

# Daemon REST API
curl http://localhost:9090/api/config   # daemon config
curl http://localhost:9090/api/sources  # discovered streams (SAP)
curl http://localhost:9090/api/sinks    # active subscriptions
curl http://localhost:9090/api/ptp      # PTP sync status
```

---

## Changing the PTP domain

Edit `/etc/aes67-daemon/daemon.conf` and set `"ptp_domain"` to match your
network (check the `a=clock-domain:PTPv2 <N>` line in your SDP, or ask your
network administrator).

Also update `/etc/linuxptp/ptp4l.conf` if needed, then restart both services:

```bash
sudo systemctl restart ptp4l aes67-daemon
```

---

## PTP notes for VirtualBox

VirtualBox does not expose hardware PTP timestamps to the guest, so `ptp4l`
runs in software-timestamp mode. This is sufficient for a **monitoring**
application — you are receiving, not generating clocks.

The aes67-daemon's `ignore_refclk_gmid` flag is set to `true` by default in
subscriptions created via the UI, which relaxes the strict PTP grandmaster
identity check. This prevents spurious subscription failures when the software
PTP offset is large.

On bare metal with a PTP-capable NIC, remove the `time_stamping software`
line from `/etc/linuxptp/ptp4l.conf` for hardware-timestamped PTP.

---

## Project layout

```
2110-mons/
├── backend/
│   ├── main.py            FastAPI: REST API, WebSocket (meters + audio), static files
│   ├── audio_monitor.py   ALSA capture thread + metering pipeline
│   ├── audio_streamer.py  Thread→asyncio bridge for browser audio WebSocket
│   ├── meters.py          PPM, EBU R128, phase correlation
│   ├── sap_listener.py    SAP/SDP multicast stream discovery (239.255.255.255:9875)
│   ├── sdp_parser.py      AES67 / ST 2110 SDP parser (handles DUP redundancy)
│   ├── ravenna_api.py     aes67-linux-daemon REST client (subscribe/unsubscribe)
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   └── js/
│       ├── app.js          UI logic, meter WebSocket, SDP modal, audio player
│       ├── meters.js       PPM bar rendering helpers
│       └── audio-worklet.js  AudioWorklet ring-buffer processor
└── setup/
    ├── vm-setup.sh           System packages, snd-aloop, ptp4l, Python env, service
    └── install-aes67-daemon.sh  Build + install aes67-linux-daemon from source
```
