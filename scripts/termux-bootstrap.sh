#!/data/data/com.termux/files/usr/bin/bash
# termux-bootstrap.sh — one-shot on-device setup for the hack-house mobile
# operator. Run this IN TERMUX on the phone (not on the laptop):
#
#   bash ~/hh-mobile/scripts/termux-bootstrap.sh
#
# It installs the deps that actually work on aarch64 Termux (cryptography from
# the package repo to dodge a rust build; pure/wheeled pip deps; NO `srp` — the
# pure-Python shim covers both client and server), holds a wake-lock so Android
# doesn't kill sshd, installs the `hh` alias, and runs the Phase-0 preflight.
#
# Idempotent: safe to re-run. Env: HH_ROOT (default ~/hh-mobile).
set -uo pipefail

ROOT="${HH_ROOT:-$HOME/hh-mobile}"
BASHRC="$HOME/.bashrc"
ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
step(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

[ -d "$ROOT" ] || { echo "hh-mobile tree not found at $ROOT — sync it first:"; \
  echo "  (laptop) tar czf - cmd_chat scripts requirements-operator.txt | \\"; \
  echo "     ssh -i ~/.ssh/<phone-key> -p 8022 <user>@<phone-tailnet-ip> 'mkdir -p ~/hh-mobile && tar xzf - -C ~/hh-mobile'"; \
  exit 1; }

step "Termux packages"
# python-cryptography from the repo avoids compiling the rust cryptography wheel
for p in python tmux git python-cryptography openssh; do
  if dpkg -s "$p" >/dev/null 2>&1 || pkg list-installed 2>/dev/null | grep -q "^$p/"; then
    ok "$p present"
  else
    warn "installing $p…"; yes | pkg install "$p" >/dev/null 2>&1 && ok "$p installed" || warn "$p install FAILED (continuing)"
  fi
done

step "Python operator deps (pure/wheeled — no srp C-ext)"
_py_has(){ python -c "import $1" >/dev/null 2>&1; }
if _py_has requests && _py_has rich && _py_has websockets; then
  ok "requests rich websockets importable"
else
  warn "installing requests rich websockets…"
  pip install --quiet requests rich websockets && ok "pip deps installed" || warn "pip install had errors"
fi
if python -c "from cryptography.fernet import Fernet; Fernet(Fernet.generate_key())" >/dev/null 2>&1; then
  ok "cryptography (Fernet) importable"
else
  warn "cryptography import FAILED — run: pkg install python-cryptography"
fi

step "Wake-lock (keep Termux/sshd alive when backgrounded)"
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock && ok "wake-lock held" || warn "wake-lock failed"
else
  warn "termux-wake-lock not found (install Termux:API for it) — Android may kill sshd"
fi

step "hh launcher alias"
if grep -q "alias hh=" "$BASHRC" 2>/dev/null; then
  ok "alias already in $BASHRC"
else
  printf "\n# hack-house mobile launcher\nalias hh='bash \"%s/scripts/hh\"'\n" "$ROOT" >> "$BASHRC"
  ok "added 'hh' alias to $BASHRC (run: source $BASHRC)"
fi

step "Phase-0 preflight"
if [ -f "$ROOT/scripts/termux-preflight.py" ]; then
  python "$ROOT/scripts/termux-preflight.py" || warn "preflight reported issues (see above)"
else
  warn "scripts/termux-preflight.py not in the synced tree — skipped"
fi

step "Next steps"
cat <<EOF
  source $BASHRC        # activate the 'hh' alias now
  hh host               # host a room on this phone (:8799, password hh-mobile)
  hh web                # mobile web console (:8790)
  hh status             # what's running
  See docs/mobile-quickstart.md for the full grammar.
EOF
