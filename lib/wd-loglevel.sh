# lib/wd-loglevel.sh — contract §11 log-level resolution for bash daemons.
#
# Source this from long-lived bash daemons (wd-decode, wd-post, …) to honor
# WSPRDAEMON_LOG_LEVEL / CLIENT_LOG_LEVEL on startup and on SIGHUP.
#
# Resolution precedence (highest first):
#   1. --log-level CLI flag              (caller must parse; sets WD_LOG_LEVEL_CLI)
#   2. WSPRDAEMON_LOG_LEVEL env var
#   3. CLIENT_LOG_LEVEL env var
#   4. Default: INFO
#
# On SIGHUP, re-sources /etc/sigmond/coordination.env (if readable) so sigmond
# can flip verbosity without restarting the RTP stream.

_WD_COORDINATION_ENV="/etc/sigmond/coordination.env"

wd_loglevel_apply() {
    if [[ -r "${_WD_COORDINATION_ENV}" ]]; then
        set -a
        # shellcheck disable=SC1090
        . "${_WD_COORDINATION_ENV}"
        set +a
    fi
    local lvl="${WD_LOG_LEVEL_CLI:-${WSPRDAEMON_LOG_LEVEL:-${CLIENT_LOG_LEVEL:-INFO}}}"
    lvl="${lvl^^}"
    case "${lvl}" in
        DEBUG)                        set -x ;;
        INFO|WARNING|ERROR|CRITICAL)  set +x ;;
        *)                            set +x ; lvl="INFO" ;;
    esac
    export WD_LOG_LEVEL="${lvl}"
}

wd_loglevel_apply
trap wd_loglevel_apply HUP
