#!/usr/bin/env bash
# One-command install on a fresh Ubuntu 24.04 server (run from the trading-agent directory as root):
#
#   sudo bash scripts/setup_vps.sh demo        # synthetic feed, PAPER mode (no Solana account needed)
#   sudo bash scripts/setup_vps.sh research    # real Pump.fun data, records + labels, never trades
#   sudo bash scripts/setup_vps.sh paper       # real data, hypothetical trades
#
# Installs PostgreSQL 16, Python 3.12 venv, Node 22; creates the database; writes .env with random tokens;
# runs migrations; builds the dashboard; installs systemd services (ta-worker, ta-api, ta-dashboard) that
# restart on failure and on reboot. Re-running is safe: existing .env values are kept.
#
# The dashboard listens on port 3000 and is firewalled so that it is reachable ONLY over Tailscale (private
# network between your phone and the server). It is never exposed to the public internet by this script.
set -euo pipefail

MODE="${1:-demo}"
case "$MODE" in
  demo)     WORKER_ARGS="--config config/paper.yaml worker --source synthetic" ;;
  research) WORKER_ARGS="--config config/research.yaml worker --source ws" ;;
  paper)    WORKER_ARGS="--config config/paper.yaml worker --source ws" ;;
  *) echo "usage: $0 demo|research|paper"; exit 1 ;;
esac

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
cd "$APP_DIR"
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv postgresql postgresql-contrib git curl ufw openssl ca-certificates >/dev/null
if ! command -v node >/dev/null || [[ "$(node -v | cut -d. -f1)" != "v22" ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi

log "Configuring PostgreSQL"
systemctl enable --now postgresql >/dev/null
touch .env && chmod 600 .env && chown "$RUN_USER" .env
getv() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
setv() {  # set KEY=VALUE in .env (replace or append)
  if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
DB_PASS="$(getv POSTGRES_PASSWORD)"; [[ -n "$DB_PASS" ]] || DB_PASS="$(openssl rand -hex 16)"
sudo -u postgres psql -qtc "SELECT 1 FROM pg_roles WHERE rolname='trading'" | grep -q 1 \
  || sudo -u postgres psql -qc "CREATE ROLE trading LOGIN PASSWORD '$DB_PASS'"
sudo -u postgres psql -qc "ALTER ROLE trading PASSWORD '$DB_PASS'" >/dev/null
sudo -u postgres psql -qtc "SELECT 1 FROM pg_database WHERE datname='trading_agent'" | grep -q 1 \
  || sudo -u postgres createdb -O trading trading_agent

log "Writing .env (existing values are kept)"
setv POSTGRES_PASSWORD "$DB_PASS"
setv DATABASE_URL "postgresql+asyncpg://trading:$DB_PASS@127.0.0.1:5432/trading_agent"
[[ -n "$(getv API_ADMIN_TOKEN)" ]] || setv API_ADMIN_TOKEN "$(openssl rand -hex 32)"
[[ -n "$(getv API_READ_TOKEN)" ]] || setv API_READ_TOKEN "$(openssl rand -hex 32)"
setv TA_API_READ_TOKEN "$(getv API_READ_TOKEN)"
setv TA_API_URL "http://127.0.0.1:8080"
if [[ -z "$(getv DASHBOARD_BASIC_AUTH)" || "$(getv DASHBOARD_BASIC_AUTH)" == "operator:CHANGE_ME" ]]; then
  setv DASHBOARD_BASIC_AUTH "admin:$(openssl rand -hex 8)"
fi
setv TA_WORKER_ARGS "$WORKER_ARGS"
if [[ "$MODE" != "demo" && -z "$(getv SOLANA_WS_URLS)" ]]; then
  echo; echo "!! Mode '$MODE' needs SOLANA_RPC_URLS and SOLANA_WS_URLS in $APP_DIR/.env (paid RPC provider)."
  echo "!! Add them (nano .env) and re-run: sudo bash scripts/setup_vps.sh $MODE"; exit 1
fi

log "Python environment"
sudo -u "$RUN_USER" python3.12 -m venv .venv
sudo -u "$RUN_USER" .venv/bin/pip install -q --upgrade pip
sudo -u "$RUN_USER" .venv/bin/pip install -q -e .

log "Database migrations"
sudo -u "$RUN_USER" bash -c "set -a; source .env; set +a; .venv/bin/ta migrate" >/dev/null

log "Building dashboard"
( cd apps/dashboard && sudo -u "$RUN_USER" npm ci --no-audit --no-fund --silent && sudo -u "$RUN_USER" npm run build >/dev/null )

log "Installing systemd services"
unit() {  # name, description, exec, extra
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$3
EnvironmentFile=$APP_DIR/.env
ExecStart=$4
Restart=on-failure
RestartSec=5
TimeoutStopSec=90
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}
unit ta-api       "Trading agent API"       "$APP_DIR" "$APP_DIR/.venv/bin/ta api"
unit ta-worker    "Trading agent worker"    "$APP_DIR" "/bin/bash -c 'exec $APP_DIR/.venv/bin/ta \$TA_WORKER_ARGS'"
unit ta-dashboard "Trading agent dashboard" "$APP_DIR/apps/dashboard" "$(command -v npm) start"
systemctl daemon-reload
systemctl enable ta-api ta-worker ta-dashboard >/dev/null 2>&1
systemctl restart ta-api ta-worker ta-dashboard

log "Firewall: SSH open; dashboard only via Tailscale"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
if command -v tailscale >/dev/null; then ufw allow in on tailscale0 to any port 3000 proto tcp >/dev/null; fi
ufw --force enable >/dev/null

sleep 8
log "Status"
systemctl --no-pager --lines=0 status ta-worker ta-api ta-dashboard | grep -E "●|Active:" || true
TS_IP="$(command -v tailscale >/dev/null && tailscale ip -4 2>/dev/null | head -1 || true)"
echo
echo "Mode:        $MODE"
echo "Dashboard:   http://${TS_IP:-<tailscale-ip>}:3000"
echo "Login:       $(getv DASHBOARD_BASIC_AUTH | sed 's/:/  \/  password: /' | sed 's/^/user: /')"
echo "Admin token: (in .env as API_ADMIN_TOKEN — needed for KILL / pause buttons)"
[[ -n "$TS_IP" ]] || echo "Tailscale not installed yet: run  curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up  then re-run this script."
echo "Logs:        journalctl -u ta-worker -f"
