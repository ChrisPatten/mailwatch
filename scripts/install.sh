#!/usr/bin/env bash
# mailwatch installer (macOS). Safe to re-run.
#   scripts/install.sh            venv + deps + config.toml + keychain items + doctor
#   scripts/install.sh --load     ...and install/reload the launchd job (every 15 min)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for c in "$HOME/.pyenv/shims/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [[ -x "$c" ]] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
  done
fi
[[ -n "$PY" ]] || { echo "need Python 3.10+"; exit 1; }
echo "python: $PY ($("$PY" --version))"

[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e '.[ocr,dev]'

mkdir -p logs raw
if [[ ! -f config.toml ]]; then
  cp config.example.toml config.toml
  echo "created config.toml: edit [owner], [[accounts]] and [paths].out, then re-run this script"
  exit 0
fi
CONFIG="$ROOT/config.toml"

# Output dir + an empty rules.yaml for tier overrides.
OUT="$(.venv/bin/python -c 'from mailwatch.config import load; print(load("config.toml").out)')"
mkdir -p "$OUT"
[[ -f "$OUT/rules.yaml" ]] || cat > "$OUT/rules.yaml" <<'YAML'
# Tier overrides your agent (or you) can maintain.
# Tiers: priority, personal, transactional, bulk_relevant, marketing, spam
senders: {}
domains: {}
# Domains mailwatch never fetches (in addition to [fetch].skip_domains in config.toml).
no_fetch_domains: []
YAML

# Every keychain:// reference in config.toml that doesn't exist yet gets a prompt.
# Not `security ... -w` with no value: its interactive prompt silently truncates at 128 characters,
# which breaks long API keys.
.venv/bin/python - <<'PY' > .keychain-refs
from mailwatch.config import load
c = load("config.toml")
refs = [(a.secret, f"IMAP password for {a.username} ({a.host})") for a in c.accounts]
refs += [(v, f"{k} API key") for k, v in c.keys.items()]
for ref, label in refs:
    if ref.startswith("keychain://"):
        svc, acct = ref[len("keychain://"):].split("/", 1)
        print(f"{svc}\t{acct}\t{label}")
PY
while IFS=$'\t' read -r svc acct label; do
  if /usr/bin/security find-generic-password -s "$svc" -a "$acct" >/dev/null 2>&1; then
    echo "keychain $svc/$acct: present"
    continue
  fi
  read -r -p "$label — add to keychain now? [y/N] " yn </dev/tty
  [[ "$yn" =~ ^[Yy] ]] || { echo "  skipped $svc/$acct"; continue; }
  read -r -s -p "  paste value (hidden): " val </dev/tty; echo
  val="$(printf '%s' "$val" | tr -d '[:space:]')"
  [[ -n "$val" ]] || { echo "  empty, skipped"; continue; }
  /usr/bin/security add-generic-password -U -s "$svc" -a "$acct" -T /usr/bin/security -w "$val"
  echo "  stored $svc/$acct (${#val} chars)"
  unset val
done < .keychain-refs
rm -f .keychain-refs

.venv/bin/mailwatch doctor || echo "doctor reported problems above"

if [[ "${1:-}" == "--load" ]]; then
  PL="$HOME/Library/LaunchAgents/com.mailwatch.run.plist"
  SVC="gui/$(id -u)/com.mailwatch.run"
  sed -e "s#@ROOT@#$ROOT#g" -e "s#@CONFIG@#$CONFIG#g" launchd/com.mailwatch.run.plist.template > "$PL"
  plutil -lint "$PL" >/dev/null || { echo "rendered plist is invalid: $PL"; exit 1; }
  launchctl bootout "$SVC" 2>/dev/null || true
  # bootout is asynchronous; bootstrapping before it finishes can leave a half-loaded job.
  for _ in $(seq 1 20); do launchctl print "$SVC" >/dev/null 2>&1 || break; sleep 0.5; done
  launchctl enable "$SVC"
  launchctl bootstrap "gui/$(id -u)" "$PL"
  launchctl kickstart "$SVC"          # don't rely on RunAtLoad: start one run now
  sleep 2
  echo "loaded com.mailwatch.run (every 15 min, :00/:15/:30/:45). $(launchctl print "$SVC" | grep -E '^\s*runs = ' | xargs)"
  echo "Logs: $ROOT/logs/   Check later: launchctl print $SVC | grep -E 'runs|last exit'"
fi
