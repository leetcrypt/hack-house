# hh-direnv.sh — source this from a directory's .envrc to auto-host a
# hack-house room whenever you `cd` in (direnv triggers it).
#
#   # .envrc
#   source "$HOME/hh-mobile/scripts/hh-direnv.sh"
#
# The room is named after the directory. Override before sourcing:
#   export HH_PORT=8801 ; export HH_PASSWORD=hunter2
#   source "$HOME/hh-mobile/scripts/hh-direnv.sh"
#
# Auto-host is ON by default; disable with `export HH_AUTOHOST=0` (env still set).

export HH_NAME="${HH_NAME:-$(basename "$PWD")}"
export HH_PORT="${HH_PORT:-8799}"
export HH_PASSWORD="${HH_PASSWORD:-hh-mobile}"
export HH_BIND="${HH_BIND:-0.0.0.0}"

if [ "${HH_AUTOHOST:-1}" = "1" ]; then
  # idempotent: the launcher no-ops if the host window already exists
  bash "$HOME/hh-mobile/scripts/hh" host "$HH_NAME" "$HH_PORT" >/dev/null 2>&1 || true
  echo "hh: auto-hosting room '$HH_NAME' on :$HH_PORT (HH_AUTOHOST=0 to disable)" >&2
fi
