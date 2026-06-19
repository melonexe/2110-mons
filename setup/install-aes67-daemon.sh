#!/usr/bin/env bash
# =============================================================================
# Build and install aes67-linux-daemon
# https://github.com/bondagit/aes67-linux-daemon
#
# Must be run AFTER vm-setup.sh (which installs build dependencies).
# Run from the project root:
#   chmod +x setup/install-aes67-daemon.sh && ./setup/install-aes67-daemon.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
step()  { echo -e "\n${CYAN}══ $* ══${NC}"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] && error "Run as a normal user with sudo access, not root."

INSTALL_DIR="$HOME/aes67-linux-daemon"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Detect interface and IP
IFACE=$(ip route show default | awk '/^default/{print $5; exit}')
VM_IP=$(ip -4 addr show "$IFACE" | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)
[[ -z "$VM_IP" ]] && error "Cannot detect IP address on $IFACE."

info "Network interface: $IFACE  IP: $VM_IP"

# ── 1. Clone ──────────────────────────────────────────────────────────────────
step "Cloning aes67-linux-daemon"

if [[ -d "$INSTALL_DIR" ]]; then
    warn "Directory $INSTALL_DIR already exists — pulling latest."
    git -C "$INSTALL_DIR" pull
else
    git clone https://github.com/bondagit/aes67-linux-daemon.git "$INSTALL_DIR"
fi

# ── 2. Build ──────────────────────────────────────────────────────────────────
step "Building aes67-linux-daemon"

cd "$INSTALL_DIR"
mkdir -p build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"

info "Build successful."

# ── 3. Install binary ─────────────────────────────────────────────────────────
step "Installing daemon binary"

sudo install -m 755 daemon/aes67-daemon /usr/local/bin/aes67-daemon
info "Installed to /usr/local/bin/aes67-daemon"

# ── 4. ALSA loopback configuration ───────────────────────────────────────────
step "Configuring ALSA for loopback device"

# The aes67-linux-daemon writes received AES67 audio into the snd-aloop
# playback side. Our monitor app reads from the capture side.
#
# snd-aloop creates cards named "Loopback" with two devices each:
#   hw:Loopback,0  — playback (daemon writes here)
#   hw:Loopback,1  — capture  (our monitor reads from here)
#
# Add a friendly alias so users can select "aes67" in the monitor UI.

sudo tee /etc/asound.conf > /dev/null <<'EOF'
# aes67-linux-daemon loopback capture alias.
# The daemon writes received AES67 streams to hw:Loopback,0.
# Our monitor app captures from hw:Loopback,1.
#
# Use device string "aes67" in the monitor UI.

pcm.aes67 {
    type hw
    card Loopback
    device 1
    subdevice 0
}

ctl.aes67 {
    type hw
    card Loopback
}
EOF

info "ALSA config written to /etc/asound.conf"
info "Use device string  aes67  or  hw:Loopback,1  in the monitor."

# ── 5. daemon.conf ────────────────────────────────────────────────────────────
step "Writing aes67-daemon configuration"

CONF_DIR="/etc/aes67-daemon"
sudo mkdir -p "$CONF_DIR"

# We put the daemon HTTP API on port 9090 so it doesn't clash with
# our monitor web UI which runs on port 8080.
sudo tee "$CONF_DIR/daemon.conf" > /dev/null <<EOF
{
  "http_port": 9090,
  "rtsp_port": 8854,
  "log_severity": 1,
  "playout_delay": 0,
  "tic_frame_size_at_1fs": 48,
  "max_tic_frame_size": 1024,
  "sample_rate": 48000,
  "rtp_mcast_base": "239.1.0.1",
  "rtp_port": 5004,
  "ptp_domain": 0,
  "ptp_dscp": 46,
  "sap_mcast_addr": "239.255.255.255",
  "sap_interval": 30,
  "mac_addr": "",
  "ip_addr": "${VM_IP}",
  "mdns_enabled": false,
  "interface_name": "${IFACE}",
  "custom_node_id": "",
  "threads": 3,
  "alsa_playback_device": "hw:Loopback,0",
  "alsa_capture_device": "hw:Loopback,1",
  "alsa_device_name": "AES67",
  "pcm_worker_threads": 3,
  "max_sources": 64,
  "max_sinks": 64
}
EOF

info "daemon.conf written to $CONF_DIR/daemon.conf"
warn "Review $CONF_DIR/daemon.conf — especially ip_addr and ptp_domain."
warn "ptp_domain must match the PTP domain on your AES67 network (commonly 0 or 127)."

# ── 6. systemd service ────────────────────────────────────────────────────────
step "Installing aes67-daemon systemd service"

sudo tee /etc/systemd/system/aes67-daemon.service > /dev/null <<EOF
[Unit]
Description=AES67 Linux Daemon
After=network.target ptp4l.service
Wants=ptp4l.service

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/aes67-daemon --conf $CONF_DIR/daemon.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable aes67-daemon
sudo systemctl start aes67-daemon

# ── 7. Verify ────────────────────────────────────────────────────────────────
step "Verifying installation"

sleep 2

if systemctl is-active --quiet aes67-daemon; then
    info "aes67-daemon is running."
else
    warn "aes67-daemon failed to start. Check: journalctl -u aes67-daemon"
fi

# Check the REST API is responding
if command -v curl &>/dev/null; then
    if curl -sf http://localhost:9090/api/config >/dev/null 2>&1; then
        info "aes67-daemon REST API responding on http://localhost:9090"
    else
        warn "REST API not yet responding — daemon may still be starting."
        warn "Test manually: curl http://localhost:9090/api/config"
    fi
fi

# Show ALSA devices
echo ""
info "Current ALSA capture devices:"
aplay -l 2>/dev/null | grep -E "card|Loopback|AES67" || echo "  (none yet — start aes67-daemon first)"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  aes67-daemon installed and running!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo ""
echo "  Daemon REST API:  http://localhost:9090/api/config"
echo "  ALSA device:      hw:Loopback,1  (alias: aes67)"
echo "  PTP domain:       $(grep ptp_domain $CONF_DIR/daemon.conf | grep -oP '\d+')"
echo ""
echo "  In the monitor UI:"
echo "    1. Paste your SDP and click 'Subscribe & Monitor'"
echo "    2. Or select device 'aes67' and set channel count manually"
echo ""
echo "  Logs:"
echo "    journalctl -fu aes67-daemon"
echo "    journalctl -fu ptp4l"
echo ""
