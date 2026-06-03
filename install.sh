#!/usr/bin/env bash
# CrabVPN Installer
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()  { echo -e "${GREEN}✅ $*${NC}"; }
warn(){ echo -e "${YELLOW}⚠️  $*${NC}"; }
err() { echo -e "${RED}❌ $*${NC}"; exit 1; }
hdr() { echo -e "\n${CYAN}${BOLD}── $* ──${NC}"; }
ask() {                          # ask VAR "prompt" [default]
    local _var=$1 _prompt=$2 _def=${3:-}
    local _full="${_prompt}${_def:+ [${_def}]}: "
    read -rp "$(echo -e "${BOLD}${_full}${NC}")" _inp
    printf -v "$_var" '%s' "${_inp:-$_def}"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
SERVICE_NAME="crabvpn"

echo -e "${CYAN}${BOLD}"
cat <<'LOGO'
   🦀  CrabVPN  —  Installer
   ───────────────────────────
LOGO
echo -e "${NC}"

# ── 1. Python 3.10+ ───────────────────────────────────────────────────────────
hdr "Python"
command -v python3 &>/dev/null || err "python3 not found — install Python 3.10+ first"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" \
    || err "Python 3.10+ required (found $(python3 --version 2>&1))"
ok "$(python3 --version)"

# ── 2. System packages (Debian/Ubuntu only, best-effort) ─────────────────────
hdr "System packages"
if command -v apt-get &>/dev/null; then
    sudo apt-get install -y -q python3-venv python3-pip 2>/dev/null && ok "apt packages" || warn "apt skipped"
else
    warn "Not a Debian system — make sure python3-venv is available"
fi

# ── 3. Virtual environment ────────────────────────────────────────────────────
hdr "Virtual environment"
if [ ! -d venv ]; then
    python3 -m venv venv
    ok "Created venv/"
else
    ok "venv/ already exists"
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip -q
ok "pip upgraded"

# ── 4. Python packages ────────────────────────────────────────────────────────
hdr "Python packages"
pip install -q -r requirements.txt
ok "All packages installed"

# ── 5. .env ───────────────────────────────────────────────────────────────────
hdr "Configuration (.env)"
if [ -f .env ]; then
    warn ".env already exists — skipping interactive setup"
    warn "Delete .env and re-run to reconfigure"
else
    echo ""
    ask BOT_TOKEN        "🤖 Telegram Bot Token (from @BotFather)"
    ask ADMIN_CHAT_ID    "📢 Admin Telegram Chat ID (your personal chat id)"
    ask DASHBOARD_SECRET "🔑 Dashboard admin password (for /dashboard)"
    ask PORTAL_BASE_URL  "🌐 Public server URL  (e.g. https://vpn.example.com:8765)"
    ask WEBHOOK_API_PORT "🔌 Web API port" "8765"
    ask DEFAULT_EXP_DAYS "📅 Default subscription length (days)" "30"
    ask ADMIN_WHITELIST  "👥 Extra admin Telegram IDs, comma-separated (leave blank if none)" ""

    cat > .env <<EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_CHAT_ID=${ADMIN_CHAT_ID}
ADMIN_WHITELIST=${ADMIN_WHITELIST}
DASHBOARD_SECRET=${DASHBOARD_SECRET}
PORTAL_BASE_URL=${PORTAL_BASE_URL}
WEBHOOK_API_PORT=${WEBHOOK_API_PORT}
DEFAULT_EXP_DAYS=${DEFAULT_EXP_DAYS}
DEFAULT_MULTIUSER=1
PAYMENT_EXPIRE_MIN=30
CONFIG_REMARKS_PREFIX=CrabVPN
EOF
    ok ".env created"
fi

# ── 6. Database ───────────────────────────────────────────────────────────────
hdr "Database"
python3 - <<'PYEOF'
from models import init_db
init_db()
print("Database initialised / migrated OK")
PYEOF
ok "Database ready"

# ── 7. Systemd service ────────────────────────────────────────────────────────
hdr "Systemd service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python3"
RUN_USER="$(whoami)"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=CrabVPN Telegram Bot & Web API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${VENV_PYTHON} main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
ok "Service file written and enabled"

# ── 8. Start ──────────────────────────────────────────────────────────────────
hdr "Starting CrabVPN"
sudo systemctl restart "${SERVICE_NAME}"
sleep 3

if sudo systemctl is-active --quiet "${SERVICE_NAME}"; then
    ok "CrabVPN is running!"
    PORTAL_URL="$(grep -E '^PORTAL_BASE_URL=' .env | cut -d= -f2-)"
    echo ""
    echo -e "${GREEN}${BOLD}🦀 Installation complete!${NC}"
    echo -e "   Dashboard : ${CYAN}${PORTAL_URL}/dashboard${NC}"
    echo -e "   Logs      : ${CYAN}journalctl -u ${SERVICE_NAME} -f${NC}"
    echo -e "   Stop      : ${CYAN}sudo systemctl stop ${SERVICE_NAME}${NC}"
    echo -e "   Restart   : ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}"
else
    warn "Service may not have started yet. Check:"
    echo -e "   ${CYAN}journalctl -u ${SERVICE_NAME} -n 50 --no-pager${NC}"
fi
