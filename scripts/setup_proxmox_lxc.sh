#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/glassscout/app"
APP_HOME="/opt/glassscout"
APP_USER="glassscout"
SERVICE="glassscout.service"
WITH_BROWSER=0

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/setup_proxmox_lxc.sh [--with-browser]

Installs GlassScout in a Debian LXC, creates its Python environment and systemd
service, and optionally installs the Playwright browser used for difficult pages.
Run this script from a checked-out GlassScout repository.
EOF
}

for argument in "$@"; do
    case "$argument" in
        --with-browser) WITH_BROWSER=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $argument" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer as root inside the LXC." >&2
    exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "${SOURCE_DIR}/app.py" || ! -f "${SOURCE_DIR}/requirements.txt" ]]; then
    echo "Run this script from a complete GlassScout checkout." >&2
    exit 1
fi

echo "[1/7] Installing Debian packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl nmap python3 python3-pip python3-venv

echo "[2/7] Creating the service account"
if ! id "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "${APP_HOME}" --shell /usr/sbin/nologin "${APP_USER}"
fi
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "${APP_HOME}" "${APP_DIR}" "${APP_DIR}/data"

echo "[3/7] Installing application files"
if [[ "${SOURCE_DIR}" != "${APP_DIR}" ]]; then
    tar --exclude='.git' --exclude='.venv' --exclude='venv' --exclude='.DS_Store' \
        --exclude='.streamlit/secrets.toml' \
        --exclude='data/*.json' --exclude='data/*.lock' --exclude='data/discovery' \
        -C "${SOURCE_DIR}" -cf - . | tar -C "${APP_DIR}" -xf -
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "[4/7] Creating the Python environment"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    runuser -u "${APP_USER}" -- python3 -m venv "${APP_DIR}/.venv"
fi
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [[ "${WITH_BROWSER}" -eq 1 ]]; then
    echo "Installing optional browser support"
    runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements-browser.txt"
    "${APP_DIR}/.venv/bin/python" -m playwright install-deps chromium
    HOME="${APP_HOME}" runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/python" -m playwright install chromium
fi

echo "[5/7] Installing the systemd service"
cat >"/etc/systemd/system/${SERVICE}" <<EOF
[Unit]
Description=GlassScout dashboard
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=HOME=${APP_HOME}
ExecStart=${APP_DIR}/.venv/bin/python -m streamlit run app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true
Restart=on-failure
RestartSec=3
LoadCredentialEncrypted=GROQ_API_KEY
LoadCredentialEncrypted=GEMINI_API_KEY

[Install]
WantedBy=multi-user.target
EOF

if [[ ! -f "${APP_DIR}/.streamlit/secrets.toml" ]]; then
    install -o "${APP_USER}" -g "${APP_USER}" -m 0600 \
        "${APP_DIR}/.streamlit/secrets.toml.example" "${APP_DIR}/.streamlit/secrets.toml"
fi
# API keys must never remain in the plaintext Streamlit configuration.
sed -i -E '/^[[:space:]]*(GROQ_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY)[[:space:]]*=/d' \
    "${APP_DIR}/.streamlit/secrets.toml"

echo "[6/7] Preparing encrypted credential storage"
install -d -m 0700 /etc/credstore.encrypted
systemd-creds setup

configure_credential() {
    local credential="$1"
    local label="$2"
    local answer
    read -r -p "Configure ${label} now? [y/N] " answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        systemd-ask-password -n "${label}:" | systemd-creds encrypt --with-key=host \
            --name="${credential}" - "/etc/credstore.encrypted/${credential}"
        chmod 0600 "/etc/credstore.encrypted/${credential}"
    fi
}

if [[ -t 0 ]]; then
    configure_credential "GROQ_API_KEY" "Groq API key"
    configure_credential "GEMINI_API_KEY" "Gemini API key"
fi

echo "[7/7] Starting GlassScout"
systemctl daemon-reload
systemctl enable --now "${SERVICE}"
systemctl --no-pager --full status "${SERVICE}"

address="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "GlassScout is ready at http://${address:-LXC-IP}:8501"
echo "API keys are stored as encrypted systemd credentials and are not shown in the dashboard."
