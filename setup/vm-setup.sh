#!/usr/bin/env bash
# =============================================================================
# AES67 / SMPTE ST 2110 Monitor — Ubuntu 22.04 VM setup
#
# Run once as a non-root user with sudo:
#   chmod +x setup/vm-setup.sh && ./setup/vm-setup.sh
#
# What this script does:
#   1. Installs all system packages (Python, ALSA, build tools, PTP)
#   2. Loads snd-aloop kernel module (virtual ALSA loopback for aes67-daemon)
#   3. Tunes network buffers for AES67 multicast traffic
#   4. Configures and starts ptp4l (IEEE 1588 slave)
#   5. Creates the Python virtualenv and installs pip packages
#   6. Installs and starts the aes67-monitor systemd service
#
# Run setup/install-aes67-daemon.sh FIRST to build the aes67-linux-daemon.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
step()  { echo -e "\n${CYAN}══ $* ══${NC}"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -eq 0 ]] && error "Run as a normal user with sudo access, not root."

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
info "App directory: $APP_DIR"

# ── Detect primary network interface ─────────────────────────────────────────
# This should be the NIC carrying your AES67 traffic.
# In VirtualBox with a bridged adapter this is typically eth0 or enp0s3.
IFACE=$(ip route show default | awk '/^default/{print $5; exit}')
[[ -z "$IFACE" ]] && error "Cannot detect default network interface. Set IFACE manually."
warn "Using network interface: $IFACE"
warn "If AES67 traffic is on a different NIC, re-run with: IFACE=eth1 ./vm-setup.sh"
echo

# ── 1. System packages ────────────────────────────────────────────────────────
step "Installing system packages"
sudo apt-get update -qq

sudo apt-get install -y \
    build-essential cmake g++ git pkg-config \
    python3 python3-pip python3-venv python3-dev \
    libasound2-dev \
    libboost-dev libboost-system-dev \
    libavahi-client-dev libavahi-common-dev \
    libsamplerate0-dev \
    linuxptp \
    iproute2 net-tools ethtool \
    --no-install-recommends

info "System packages installed."

# ── 2. snd-aloop (virtual ALSA loopback) ─────────────────────────────────────
step "Loading snd-aloop kernel module"
# The aes67-linux-daemon writes received AES67 audio into an ALSA loopback
# device. Our monitor app reads from the other side of that loopback.

if lsmod | grep -q snd_aloop; then
    info "snd-aloop already loaded."
else
    sudo modprobe snd-aloop
    info "snd-aloop loaded."
fi

# Persist across reboots
if ! grep -q "snd-aloop" /etc/modules 2>/dev/null; then
    echo "snd-aloop" | sudo tee -a /etc/modules > /dev/null
fi

# Set loopback subdevice count (one per AES67 sink, up to 8)
sudo tee /etc/modprobe.d/snd-aloop.conf > /dev/null <<'EOF'
options snd-aloop enable=1 index=1 pcm_substreams=8
EOF

info "snd-aloop will load on boot with 8 substreams."

# ── 3. Network tuning ─────────────────────────────────────────────────────────
step "Tuning network for AES67 multicast"

# Large socket receive buffers to prevent RTP packet drops
sudo tee /etc/sysctl.d/90-aes67.conf > /dev/null <<'EOF'
# AES67 / SMPTE ST 2110 network tuning
net.core.rmem_max        = 268435456
net.core.rmem_default    = 268435456
net.core.netdev_max_backlog = 250000
EOF

sudo sysctl -p /etc/sysctl.d/90-aes67.conf -q
info "Socket buffer sizes increased."

# Enable multicast on the AES67 interface
sudo ip link set "$IFACE" multicast on

# Ensure multicast route exists for AES67 / SAP address space
if ! ip route show | grep -q "239.0.0.0/8"; then
    sudo ip route add 239.0.0.0/8 dev "$IFACE" || true
fi

# Persist multicast route via /etc/network/interfaces.d or netplan
# (using a post-up script that works regardless of network manager)
sudo tee /etc/networkd-dispatcher/routable.d/50-aes67-mcast > /dev/null <<EOF
#!/bin/sh
ip link set $IFACE multicast on
ip route add 239.0.0.0/8 dev $IFACE 2>/dev/null || true
EOF
sudo chmod +x /etc/networkd-dispatcher/routable.d/50-aes67-mcast

info "Multicast route configured on $IFACE."

# ── 4. PTP (IEEE 1588) ────────────────────────────────────────────────────────
step "Configuring ptp4l (IEEE 1588 slave)"

sudo mkdir -p /etc/linuxptp

# Note: VirtualBox does not expose hardware PTP timestamps, so we use
# software timestamps. On bare metal with a PTP-capable NIC, remove the
# time_stamping line and set time_stamping to hardware.
sudo tee /etc/linuxptp/ptp4l.conf > /dev/null <<EOF
[global]
slaveOnly               1
time_stamping           software
tx_timestamp_timeout    10
logging_level           5
summary_interval        60
dscp_event              46
dscp_general            34

[$IFACE]
EOF

# Ubuntu's linuxptp package ships the binary but no systemd service file,
# so we write the complete unit ourselves.
sudo tee /etc/systemd/system/ptp4l.service > /dev/null <<EOF
[Unit]
Description=IEEE 1588 Precision Time Protocol (PTP) slave
Documentation=man:ptp4l(8)
After=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/ptp4l -f /etc/linuxptp/ptp4l.conf -i $IFACE
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ptp4l
sudo systemctl start ptp4l

info "ptp4l started (slave-only, software timestamps, interface: $IFACE)."
info "Check PTP lock: journalctl -u ptp4l -f"

# ── 5. Python virtual environment ─────────────────────────────────────────────
step "Creating Python virtual environment"

VENV="$APP_DIR/.venv"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip -q
pip install -r "$APP_DIR/backend/requirements.txt" -q
deactivate

info "Python environment ready at $VENV"

# ── 6. aes67-monitor systemd service ─────────────────────────────────────────
step "Installing aes67-monitor systemd service"

sudo tee /etc/systemd/system/aes67-monitor.service > /dev/null <<EOF
[Unit]
Description=AES67 / ST 2110 Audio Monitor Web UI
After=network.target sound.target aes67-daemon.service
Wants=aes67-daemon.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR/backend
ExecStart=$VENV/bin/python main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=RAVENNA_API_URL=http://localhost:9090

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable aes67-monitor
sudo systemctl start aes67-monitor

# ── 7. Firewall ───────────────────────────────────────────────────────────────
if command -v ufw &>/dev/null && sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow 8080/tcp comment "AES67 Monitor UI" 2>/dev/null || true
    sudo ufw allow 9875/udp comment "AES67 SAP discovery" 2>/dev/null || true
    info "ufw: ports 8080 (UI) and 9875 (SAP) opened."
fi

# ── Done ─────────────────────────────────────────────────────────────────────
VM_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  System setup complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo ""
echo "  Next step: build the AES67 daemon"
echo "    ./setup/install-aes67-daemon.sh"
echo ""
echo "  Once the daemon is running, browse to:"
echo "    http://${VM_IP}:8080"
echo ""
echo "  Useful commands:"
echo "    journalctl -fu aes67-monitor    monitor app logs"
echo "    journalctl -fu aes67-daemon     aes67-linux-daemon logs"
echo "    journalctl -fu ptp4l            PTP sync status"
echo "    aplay -l                        list ALSA devices"
echo ""
