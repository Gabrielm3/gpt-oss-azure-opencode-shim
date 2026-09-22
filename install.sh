#!/usr/bin/env bash
#
# Install gpt-oss-azure-opencode-shim as a systemd user service.
# Run from the cloned repository directory.
#
set -euo pipefail

SERVICE_NAME="gpt-oss-azure-opencode-shim.service"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/gpt-oss-azure-opencode-shim.env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
INSTALL_DIR="$(pwd)"

err()  { echo "[FAIL] $*" >&2; exit 1; }
ok()   { echo "[PASS] $*"; }
info() { echo "[INFO] $*"; }

command -v python3 >/dev/null || err "python3 not found"
python3 -m venv --help >/dev/null 2>&1 \
    || err "python3-venv not available (sudo apt install -y python3-venv)"

# 1. Install into local venv
info "creating venv in $INSTALL_DIR/.venv"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet .
ok "package installed"

# 2. Env file (created only if missing)
if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    cat > "$ENV_FILE" <<ENVEOF
# Required
UPSTREAM_URL=https://YOUR_RESOURCE.services.ai.azure.com/openai
AZURE_FOUNDRY_API_KEY=replace-me

# Optional
SHIM_HOST=127.0.0.1
SHIM_PORT=9526
SHIM_LOG_LEVEL=info
ENVEOF
    chmod 600 "$ENV_FILE"
    ok "env file created: $ENV_FILE"
else
    info "env file already exists: $ENV_FILE"
fi

# 3. systemd user service (Linux with systemd only)
if command -v systemctl >/dev/null && systemctl --user >/dev/null 2>&1; then
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$SERVICE_NAME" <<UNITEOF
[Unit]
Description=gpt-oss-azure-opencode-shim
After=network.target

[Service]
Type=simple
EnvironmentFile=%h/.config/gpt-oss-azure-opencode-shim.env
ExecStart=$INSTALL_DIR/.venv/bin/gpt-oss-azure-opencode-shim
WorkingDirectory=$INSTALL_DIR
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNITEOF

    systemctl --user daemon-reload
    ok "systemd unit installed: $SERVICE_NAME"
    info "edit $ENV_FILE, then run:"
    info "  systemctl --user enable --now $SERVICE_NAME"
else
    info "systemd --user not available; run manually:"
    info "  set -a; source $ENV_FILE; set +a"
    info "  $INSTALL_DIR/.venv/bin/gpt-oss-azure-opencode-shim"
fi

echo
echo "Done."
