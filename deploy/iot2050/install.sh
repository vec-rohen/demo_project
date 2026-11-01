#!/usr/bin/env bash
# Install, update or remove the Plant HMI edge app on a Siemens IoT2050
# (or any Debian-based Linux box). Run as root from a copy of this repository:
#
#   sudo ./deploy/iot2050/install.sh                     # install or update, start the service
#   sudo ./deploy/iot2050/install.sh --wheels ./wheels    # offline: install Python packages from a folder
#   sudo ./deploy/iot2050/install.sh --package site.json  # first start with this configuration package
#   sudo ./deploy/iot2050/install.sh --uninstall          # remove the app, keep data
#   sudo ./deploy/iot2050/install.sh --uninstall --purge  # remove the app and all data
#
# Options: --port N (default 8000), --https (self-signed certificate; browsers ask
# you to accept it once), --no-service (files + Python environment only,
# e.g. inside a container), PYTHON=/usr/bin/python3.x to choose the interpreter.
#
# Re-running the script updates the app in place; the device database
# (configuration, users, trends, journal) in /var/lib/plant-hmi is kept.
set -euo pipefail

APP_DIR=/opt/plant-hmi
DATA_DIR=/var/lib/plant-hmi
SERVICE=plant-hmi
SVC_USER=plant-hmi
PORT=8000
PYTHON=${PYTHON:-python3}
WHEELS=""
PACKAGE=""
UNINSTALL=0
PURGE=0
WITH_SERVICE=1
HTTPS=0

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --wheels) WHEELS="$(cd "$2" && pwd)"; shift 2 ;;
    --package) PACKAGE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    --no-service) WITH_SERVICE=0; shift ;;
    --https) HTTPS=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) fail "unknown option $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || fail "run as root (sudo $0 $*)"
SRC_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# --- uninstall ----------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
  if command -v systemctl >/dev/null && [ -f "/etc/systemd/system/$SERVICE.service" ]; then
    say "Stopping and removing the $SERVICE service"
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
  fi
  rm -rf "$APP_DIR"
  if [ "$PURGE" -eq 1 ]; then
    say "Deleting all data in $DATA_DIR"
    rm -rf "$DATA_DIR"
    if id "$SVC_USER" >/dev/null 2>&1; then
      userdel "$SVC_USER" || echo "    Couldn't remove the $SVC_USER account (still in use?); remove it later with: userdel $SVC_USER"
    fi
  else
    say "App removed. Data kept in $DATA_DIR (use --purge to delete it)."
  fi
  exit 0
fi

# --- checks ---------------------------------------------------------------------
[ -f "$SRC_ROOT/src/hmi/server.py" ] || fail "run this script from a copy of the repository ($SRC_ROOT has no src/hmi)"
command -v "$PYTHON" >/dev/null || fail "$PYTHON not found. Install Python 3.9 or newer (apt install python3)."
PYV="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "Python $PYV is too old; 3.9 or newer is needed."
if ! "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null; then
  if command -v apt-get >/dev/null; then
    say "Installing python3-venv"
    { apt-get install -y "python${PYV}-venv" || apt-get install -y python3-venv \
      || { apt-get update && { apt-get install -y "python${PYV}-venv" || apt-get install -y python3-venv; }; }; } \
      || fail "couldn't install python3-venv; install it manually and re-run"
  else
    fail "Python's venv module is missing; install python3-venv and re-run"
  fi
fi
case "$PORT" in ''|*[!0-9]*) fail "--port must be a number" ;; esac
say "Python $PYV · installing to $APP_DIR · data in $DATA_DIR · port $PORT"

# --- service account & folders -----------------------------------------------------
if ! id "$SVC_USER" >/dev/null 2>&1; then
  say "Creating system user $SVC_USER"
  useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
install -d -m 755 "$APP_DIR"
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"

# --- application files (edge app only; the cloud portal is not needed here) ----------
say "Copying the application"
rm -rf "$APP_DIR/src.new"
mkdir -p "$APP_DIR/src.new"
cp "$SRC_ROOT/src/__init__.py" "$APP_DIR/src.new/" 2>/dev/null || touch "$APP_DIR/src.new/__init__.py"
cp -r "$SRC_ROOT/src/hmi" "$APP_DIR/src.new/hmi"
find "$APP_DIR/src.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
cp "$SRC_ROOT/deploy/iot2050/requirements-edge.txt" "$APP_DIR/requirements.txt"
rm -rf "$APP_DIR/src.old"
[ -d "$APP_DIR/src" ] && mv "$APP_DIR/src" "$APP_DIR/src.old"
mv "$APP_DIR/src.new" "$APP_DIR/src"
rm -rf "$APP_DIR/src.old"
git -C "$SRC_ROOT" rev-parse --short HEAD > "$APP_DIR/VERSION" 2>/dev/null || date -u +%Y%m%d > "$APP_DIR/VERSION"

# --- Python environment -------------------------------------------------------------
if [ ! -x "$APP_DIR/venv/bin/python" ] || ! "$APP_DIR/venv/bin/python" -c 'import sys' 2>/dev/null; then
  say "Creating Python environment"
  rm -rf "$APP_DIR/venv"
  "$PYTHON" -m venv "$APP_DIR/venv"
fi
say "Installing Python packages${WHEELS:+ from $WHEELS (offline)}"
if [ -n "$WHEELS" ]; then
  "$APP_DIR/venv/bin/pip" install --quiet --no-index --find-links "$WHEELS" -r "$APP_DIR/requirements.txt"
else
  "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
  "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"  # progress shown: slow over a modem
fi
"$APP_DIR/venv/bin/python" -c 'import fastapi, uvicorn, pymodbus, websockets, snap7' \
  || fail "Python packages didn't install correctly"
(cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -c 'import src.hmi.server') \
  || fail "the app doesn't import on this Python; see the error above"

# --- first-start package --------------------------------------------------------------
# --- HTTPS certificate (self-signed, for the plant network) ----------------------------
TLS_ARGS=""
SCHEME=http
if [ "$HTTPS" -eq 1 ]; then
  command -v openssl >/dev/null || fail "openssl is needed for --https (apt install openssl)"
  TLS_DIR="$DATA_DIR/tls"
  install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$TLS_DIR"
  if [ ! -f "$TLS_DIR/cert.pem" ]; then
    say "Creating a self-signed HTTPS certificate"
    SAN="DNS:$(hostname),DNS:localhost,IP:127.0.0.1"
    for ip in $(hostname -I 2>/dev/null); do case "$ip" in *.*) SAN="$SAN,IP:$ip" ;; esac; done
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -subj "/CN=$(hostname) Plant HMI" \
      -addext "subjectAltName=$SAN" -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" 2>/dev/null \
      || fail "couldn't create the certificate"
    chown "$SVC_USER:$SVC_USER" "$TLS_DIR/key.pem" "$TLS_DIR/cert.pem"
    chmod 600 "$TLS_DIR/key.pem"
  fi
  TLS_ARGS=" --tls-cert $TLS_DIR/cert.pem --tls-key $TLS_DIR/key.pem"
  SCHEME=https
fi

# --- network time: trends and alarm times are only as good as the device clock ----------
if command -v timedatectl >/dev/null && [ "$WITH_SERVICE" -eq 1 ]; then
  if [ "$(timedatectl show -p NTP --value 2>/dev/null)" != "yes" ]; then
    say "Turning on network time (NTP)"
    timedatectl set-ntp true 2>/dev/null || echo "    Couldn't enable NTP automatically – set up time sync on this device."
  fi
  if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" != "yes" ]; then
    echo "    Note: the clock isn't synchronised yet. It needs internet or a local NTP server; check with 'timedatectl'."
  fi
fi

PACKAGE_ENV=""
if [ -n "$PACKAGE" ]; then
  [ -f "$PACKAGE" ] || fail "package file $PACKAGE not found"
  if [ -f "$DATA_DIR/edge.db" ]; then
    say "Device already configured; --package ignored (load new packages from the Commissioning tab)"
  else
    install -m 640 -o "$SVC_USER" -g "$SVC_USER" "$PACKAGE" "$DATA_DIR/first-package.json"
    PACKAGE_ENV="Environment=HMI_PACKAGE=$DATA_DIR/first-package.json"
  fi
fi

if [ "$WITH_SERVICE" -eq 0 ]; then
  say "Installed without a service. Start it with:"
  echo "  cd $APP_DIR && sudo -u $SVC_USER env HMI_DB=$DATA_DIR/edge.db HMI_INITIAL_PASSWORD_FILE=$DATA_DIR/initial-admin-password venv/bin/python -m src.hmi --host 0.0.0.0 --port $PORT$TLS_ARGS"
  exit 0
fi

# --- systemd service ----------------------------------------------------------------
command -v systemctl >/dev/null || fail "systemd not found; re-run with --no-service and start the app yourself"
CAPS=""
[ "$PORT" -lt 1024 ] && CAPS=$'AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE'
say "Writing /etc/systemd/system/$SERVICE.service"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Plant HMI edge app (operator interface and PLC gateway)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$APP_DIR
Environment=HMI_DB=$DATA_DIR/edge.db
Environment=HMI_INITIAL_PASSWORD_FILE=$DATA_DIR/initial-admin-password
$PACKAGE_ENV
ExecStart=$APP_DIR/venv/bin/python -m src.hmi --host 0.0.0.0 --port $PORT$TLS_ARGS
Restart=always
RestartSec=5
# Hardening: the app only needs to write its own data folder, and its files
# (configuration, password hashes) are private to the service account.
UMask=0027
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=$DATA_DIR
$CAPS

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

say "Waiting for the app to start"
for _ in $(seq 1 60); do
  systemctl is-active --quiet "$SERVICE" && [ -f "$DATA_DIR/edge.db" ] && break
  sleep 1
done
systemctl is-active --quiet "$SERVICE" || { journalctl -u "$SERVICE" -n 30 --no-pager || true; fail "the service didn't start"; }

ADDRS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' || true)"
echo
say "Plant HMI is running ($(cat "$APP_DIR/VERSION"))"
for ip in ${ADDRS:-localhost}; do echo "    $SCHEME://$ip:$PORT"; done
[ "$HTTPS" -eq 1 ] && echo "    (self-signed certificate: the browser asks you to accept it the first time)"
if [ -f "$DATA_DIR/initial-admin-password" ]; then
  echo
  say "First sign-in (one-time password – you'll be asked to change it):"
  sed 's/^/    /' "$DATA_DIR/initial-admin-password"
fi
echo
echo "Logs:     journalctl -u $SERVICE -f"
echo "Restart:  systemctl restart $SERVICE"
echo "Update:   copy the new repository version over and re-run this script"
