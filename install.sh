#!/bin/bash
# /opt/wsprdaemon-client/install.sh
# wsprdaemon v4 installer
#
# Usage: sudo ./install.sh [--uninstall]
#
# Installs:
#   - wsprdaemon user/group
#   - Directory structure (FHS compliant)
#   - Executables to /usr/local/sbin/
#   - Python library to /opt/wsprdaemon-client/lib/
#   - systemd unit files to /etc/systemd/system/
#   - Config directory /etc/wsprdaemon/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/wsprdaemon-client"
SBIN_DIR="/usr/local/sbin"
SYSTEMD_DIR="/etc/systemd/system"
ETC_DIR="/etc/wsprdaemon"
SPOOL_DIR="/var/spool/wsprdaemon"
LOG_DIR="/var/log/wsprdaemon"
RUN_DIR="/run/wsprdaemon"

WD_USER="wsprdaemon"
WD_GROUP="radio"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# --- Check root ---
if [[ $EUID -ne 0 ]]; then
    error "This script must be run as root (use sudo)"
    exit 1
fi

# --- Uninstall ---
if [[ "${1:-}" == "--uninstall" ]]; then
    info "Uninstalling wsprdaemon v4..."

    # Stop and disable services
    info "Stopping services..."
    systemctl stop wsprdaemon.target 2>/dev/null || true
    for unit in "${SYSTEMD_DIR}"/wd-*.service "${SYSTEMD_DIR}"/wsprdaemon.*; do
        [[ -f "${unit}" ]] && systemctl disable "$(basename "${unit}")" 2>/dev/null || true
    done

    # Remove unit files
    info "Removing unit files..."
    rm -f "${SYSTEMD_DIR}"/wd-ka9q-record@.service
    rm -f "${SYSTEMD_DIR}"/wd-kiwi-record@.service
    rm -f "${SYSTEMD_DIR}"/wd-decode@.service
    rm -f "${SYSTEMD_DIR}"/wd-post@.service
    rm -f "${SYSTEMD_DIR}"/wsprdaemon.target
    systemctl daemon-reload

    # Remove executables
    info "Removing executables..."
    rm -f "${SBIN_DIR}"/wd-ctl
    rm -f "${SBIN_DIR}"/wd-ka9q-record
    rm -f "${SBIN_DIR}"/wd-kiwi-record
    rm -f "${SBIN_DIR}"/wd-kiwi-cleanup
    rm -f "${SBIN_DIR}"/wd-decode
    rm -f "${SBIN_DIR}"/wd-post

    info "Uninstall complete."
    info "Config in ${ETC_DIR}, logs in ${LOG_DIR}, and spool in ${SPOOL_DIR} were preserved."
    info "Remove them manually if desired."
    exit 0
fi

# --- Install ---
info "Installing wsprdaemon v4..."

# --- Runtime system packages ---
# Hard runtime dependencies of the wd-* scripts.  Without these:
#   - inotify-tools : wd-decode silently busy-loops (no inotifywait → command
#     not found is swallowed, while loop spins ~100 forks/sec per band).
#   - sox           : wd-decode noise-window stats (RMS/Pk/peak-level dB).
#   - curl          : upload paths and a few diag scripts.
if command -v apt-get >/dev/null 2>&1; then
    info "Installing runtime system packages (inotify-tools, sox, curl)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        inotify-tools sox curl
fi

# Create radio group if needed (must exist before useradd --groups)
if ! getent group "${WD_GROUP}" &>/dev/null; then
    info "Creating group ${WD_GROUP}..."
    groupadd --system "${WD_GROUP}"
fi

# Create user if needed
if ! id "${WD_USER}" &>/dev/null; then
    info "Creating user ${WD_USER}..."
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --groups "${WD_GROUP}" "${WD_USER}" 2>/dev/null || \
    useradd --system --no-create-home --shell /usr/sbin/nologin "${WD_USER}"
    # Ensure user is in the radio group
    usermod -a -G "${WD_GROUP}" "${WD_USER}" 2>/dev/null || true
fi

# Make sure ${WD_USER}'s HOME is usable.  Even with --no-create-home above,
# /etc/passwd lists /home/wsprdaemon as the user's HOME, and wd-upload-
# wsprdaemon writes an SSH keypair there for SFTP auth to wsprdaemon.org.
# Without the directory (or with root ownership), the upload service emits
# `Permission denied: '/home/wsprdaemon/.ssh'` on every cycle.
WSPRDAEMON_HOME="$(getent passwd "${WD_USER}" | cut -d: -f6)"
if [[ -n "${WSPRDAEMON_HOME}" ]]; then
    mkdir -p "${WSPRDAEMON_HOME}"
    chown "${WD_USER}:${WD_USER}" "${WSPRDAEMON_HOME}"
    chmod 0750 "${WSPRDAEMON_HOME}"
fi

# Create directories
info "Creating directories..."
dirs=(
    "${ETC_DIR}"
    "${ETC_DIR}/env"
    "${SPOOL_DIR}"
    "${SPOOL_DIR}/recording"
    "${SPOOL_DIR}/posting"
    "${SPOOL_DIR}/posting/uploads/wsprnet"
    "${SPOOL_DIR}/posting/uploads/wsprdaemon"
    "${LOG_DIR}"
    "${INSTALL_DIR}"
    "${INSTALL_DIR}/lib"
)

for d in "${dirs[@]}"; do
    mkdir -p "${d}"
done

# Set ownership
chown -R "${WD_USER}:${WD_GROUP}" "${SPOOL_DIR}"
chown -R "${WD_USER}:${WD_GROUP}" "${LOG_DIR}"
chown -R root:"${WD_GROUP}" "${ETC_DIR}"
chmod 2775 "${ETC_DIR}"

# /var/log/wspr.log is the KA9Q-standard append-only spot log that
# wd-upload-wsprnet writes to after each successful upload.  Pre-create
# it with wsprdaemon ownership so the service can append without
# emitting a permission-denied warning on every cycle (the warning is
# noisy but harmless — actual uploads still succeed via journal-only
# logging).  Empty file is fine; the uploader rotates it itself once
# it exceeds WSPR_LOG_MAX.
install -m 0644 -o "${WD_USER}" -g "${WD_GROUP}" /dev/null /var/log/wspr.log
chmod 2775 "${ETC_DIR}/env"

# Install Python library
info "Installing wdlib..."
cp -r "${SCRIPT_DIR}/lib/wdlib" "${INSTALL_DIR}/lib/"

# Install Python recorder
mkdir -p "${INSTALL_DIR}/bin"
cp "${SCRIPT_DIR}/bin/wd-ka9q-record.py" "${INSTALL_DIR}/bin/wd-ka9q-record.py"

# Install deps.conf so wd-ctl can resolve source-tree paths when installed
cp "${SCRIPT_DIR}/deps.conf" "${INSTALL_DIR}/deps.conf"

# Install decoder binaries into /opt/wsprdaemon-client/bin/decoders/ (all arches).
# wd-decode resolves the right binary at runtime via arch detection.
# These are versioned project binaries — do NOT install to /usr/local/sbin/.
info "Installing decoder binaries to ${INSTALL_DIR}/bin/decoders/..."
mkdir -p "${INSTALL_DIR}/bin/decoders"
install -m 755 "${SCRIPT_DIR}/bin/decoders/wsprd-x86-v27"   "${INSTALL_DIR}/bin/decoders/"
install -m 755 "${SCRIPT_DIR}/bin/decoders/wsprd-arm64-v27" "${INSTALL_DIR}/bin/decoders/"
install -m 755 "${SCRIPT_DIR}/bin/decoders/wsprd-armhf-v26" "${INSTALL_DIR}/bin/decoders/"
install -m 755 "${SCRIPT_DIR}/bin/decoders/jt9-x86-v27"     "${INSTALL_DIR}/bin/decoders/"
install -m 755 "${SCRIPT_DIR}/bin/decoders/jt9-x86-v26"     "${INSTALL_DIR}/bin/decoders/"
install -m 755 "${SCRIPT_DIR}/bin/decoders/jt9-arm64-v27"   "${INSTALL_DIR}/bin/decoders/"
install -m 755 "${SCRIPT_DIR}/bin/decoders/jt9-arm32-v26"   "${INSTALL_DIR}/bin/decoders/"

# Install executables
info "Installing executables to ${SBIN_DIR}..."
install -m 755 "${SCRIPT_DIR}/bin/wd-ctl" "${SBIN_DIR}/wd-ctl"
install -m 755 "${SCRIPT_DIR}/bin/wd-ka9q-record" "${SBIN_DIR}/wd-ka9q-record"
install -m 755 "${SCRIPT_DIR}/bin/wd-kiwi-record" "${SBIN_DIR}/wd-kiwi-record"
install -m 755 "${SCRIPT_DIR}/bin/wd-kiwi-cleanup" "${SBIN_DIR}/wd-kiwi-cleanup"
install -m 755 "${SCRIPT_DIR}/bin/wd-decode" "${SBIN_DIR}/wd-decode"
install -m 755 "${SCRIPT_DIR}/bin/wd-post" "${SBIN_DIR}/wd-post"
install -m 755 "${SCRIPT_DIR}/bin/wd-upload-hs" "${SBIN_DIR}/wd-upload-hs"

# Install systemd unit files
info "Installing systemd units to ${SYSTEMD_DIR}..."
install -m 644 "${SCRIPT_DIR}/systemd/wd-ka9q-record@.service" "${SYSTEMD_DIR}/"
install -m 644 "${SCRIPT_DIR}/systemd/wd-kiwi-record@.service" "${SYSTEMD_DIR}/"
install -m 644 "${SCRIPT_DIR}/systemd/wd-decode@.service" "${SYSTEMD_DIR}/"
install -m 644 "${SCRIPT_DIR}/systemd/wd-post@.service" "${SYSTEMD_DIR}/"
install -m 644 "${SCRIPT_DIR}/systemd/wd-upload-hs@.service" "${SYSTEMD_DIR}/"
install -m 644 "${SCRIPT_DIR}/systemd/wsprdaemon.target" "${SYSTEMD_DIR}/"

# Install tmpfiles.d cleanup rule for the legacy wsprnet upload spool.
# After WSPR_USE_HS_UPLOADER=1 flips the upload path to wd-upload-hs,
# wd-post still writes _spots.txt files into the wsprnet/ subdirectory
# (its behavior is unchanged), but nothing reads from there — the new
# uploader pulls wspr.spots from sigmond's SQLite sink.  This entry
# applies a 24-hour TTL on those stale files via the daily
# systemd-tmpfiles-clean.timer.
install -m 644 "${SCRIPT_DIR}/tmpfiles.d/wsprdaemon-wsprnet-spool-cleanup.conf" \
    /etc/tmpfiles.d/

# Reload systemd
systemctl daemon-reload
systemd-tmpfiles --create /etc/tmpfiles.d/wsprdaemon-wsprnet-spool-cleanup.conf 2>/dev/null || true

# --- Python venv + dependency installation ---------------------------------
# wd-ctl install-deps creates /opt/wsprdaemon-client/venv and pip-installs
# the deps listed in deps.conf (ka9q-python, wspr-recorder, etc.).  Without
# this step, `wd-ka9q-record@*.service` fails with "venv/bin/python3: No
# such file or directory" the first time radiod hands it a stream.  Run it
# here so `smd install wsprdaemon-client` (or a hand `./install.sh`)
# produces a fully-runnable install — operators no longer have to remember
# to follow up with a separate `wd-ctl install-deps`.
info ""
info "=== Installing Python dependencies (wd-ctl install-deps) ==="
if "${SBIN_DIR}/wd-ctl" install-deps; then
    info "Python venv ready at /opt/wsprdaemon-client/venv"
else
    warn "wd-ctl install-deps failed — wd-ka9q-record will not start."
    warn "Re-run manually with:  sudo wd-ctl install-deps"
fi

# --- hs-uploader system install + tmpfiles.d entry ------------------------
# wd-upload-hs uses the system /usr/bin/python3 (via "#!/usr/bin/env
# python3" shebang) and needs to import `hs_uploader`.  sigmond's own
# install.sh installs sigmond at system level via an editable .pth in
# /usr/local/lib/python3.*/dist-packages/; we mirror that pattern here
# for hs-uploader so wsprdaemon-client deployments don't fail on a
# missing module right after install.  --break-system-packages is
# acceptable for an /opt/git/sigmond/ editable install (the package
# isn't a debian apt-managed one).
HS_UPLOADER_REPO=""
for candidate in "/opt/git/sigmond/hs-uploader" "${SCRIPT_DIR}/../hs-uploader"; do
    if [[ -f "${candidate}/pyproject.toml" ]]; then
        HS_UPLOADER_REPO="${candidate}"
        break
    fi
done
if [[ -n "${HS_UPLOADER_REPO}" ]]; then
    info ""
    info "=== Installing hs-uploader (system-level editable) ==="
    info "    repo: ${HS_UPLOADER_REPO}"
    if /usr/bin/pip install --quiet --break-system-packages -e "${HS_UPLOADER_REPO}"; then
        info "hs-uploader installed; wd-upload-hs can now import it"
    else
        warn "pip install hs-uploader failed — wd-upload-hs will not start"
        warn "until the operator runs:"
        warn "  sudo pip install --break-system-packages -e ${HS_UPLOADER_REPO}"
    fi
    # Pre-create /var/lib/hs-uploader with group=sigmond + setgid so
    # multiple HamSCI client users (pskrec, wsprdaemon, ...) can share
    # the watermark store.  See tmpfiles.d/hs-uploader.conf in the
    # hs-uploader repo for rationale.
    if [[ -f "${HS_UPLOADER_REPO}/tmpfiles.d/hs-uploader.conf" ]]; then
        install -m 644 "${HS_UPLOADER_REPO}/tmpfiles.d/hs-uploader.conf" \
            /etc/tmpfiles.d/
        systemd-tmpfiles --create /etc/tmpfiles.d/hs-uploader.conf 2>/dev/null || true
    fi
else
    warn "hs-uploader repo not found at standard locations — wd-upload-hs"
    warn "will fail with 'No module named hs_uploader' until the operator"
    warn "installs it manually."
fi

# Create tmpfs fstab entry hint
info ""
info "=== Optional: tmpfs mount for recording directory ==="
info "Add this line to /etc/fstab for tmpfs performance:"
info "  tmpfs  ${SPOOL_DIR}/recording  tmpfs  size=256M,uid=${WD_USER},gid=${WD_GROUP},mode=0775  0  0"
info ""
info "The actual size should be calculated from your config."
info "Run 'wd-ctl migrate-config' to convert your v3 config."
info ""

info "Installation complete!"
info ""
info "Next steps:"
info "  1. Migrate your v3 config:"
info "     wd-ctl migrate-config /path/to/old/wsprdaemon.conf -o ${ETC_DIR}"
info "  2. Review the generated config:"
info "     less ${ETC_DIR}/wsprdaemon.conf"
info "  3. Review generated env files:"
info "     ls ${ETC_DIR}/env/"
info "  4. Apply the config (start services):"
info "     wd-ctl apply"
