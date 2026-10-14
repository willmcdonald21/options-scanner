#!/usr/bin/env bash
#
# Run and inspect the two bots that share this Mac, this IB Gateway and this
# paper account.
#
#   ./scripts/ops.sh status     where everything stands right now
#   ./scripts/ops.sh doctor     preflight: the things that have actually broken
#   ./scripts/ops.sh start|stop|restart [scanner|warrior|all]
#   ./scripts/ops.sh logs [scanner|warrior|gateway]
#
# Deliberately not `set -e`: doctor must report every failing check, not stop
# at the first one.
set -uo pipefail

SCANNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WARRIOR_DIR="${WARRIOR_DIR:-$HOME/Developer/VolatilityTrader}"
LABEL="com.willmcdonald.options-scanner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$SCANNER_DIR/.venv/bin/python"
PROBE_CLIENT_ID=19

if [[ -t 1 ]]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else
  R=''; G=''; Y=''; B=''; N=''
fi

FAILS=0; WARNS=0
ok()   { printf '  %s[ ok ]%s %s\n'   "$G" "$N" "$1"; }
warn() { printf '  %s[warn]%s %s\n'   "$Y" "$N" "$1"; WARNS=$((WARNS+1)); }
bad()  { printf '  %s[FAIL]%s %s\n'   "$R" "$N" "$1"; FAILS=$((FAILS+1)); }
note() { printf '  %s[note]%s %s\n'   "$B" "$N" "$1"; }
head_() { printf '\n%s%s%s\n' "$B" "$1" "$N"; }

# --- configuration readers ------------------------------------------------

scanner_cfg() {  # scanner_cfg <dotted.path> [default]
  "$PY" - "$1" "${2:-}" <<'PYEOF' 2>/dev/null
import sys, yaml, pathlib
path, default = sys.argv[1], sys.argv[2]
try:
    data = yaml.safe_load(pathlib.Path("config.yaml").read_text()) or {}
    for part in path.split("."):
        data = data[part]
    print(data)
except Exception:
    print(default)
PYEOF
}

warrior_cfg() {
  "$PY" - "$1" "${2:-}" "$WARRIOR_DIR" <<'PYEOF' 2>/dev/null
import sys, yaml, pathlib
path, default, root = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = yaml.safe_load((pathlib.Path(root) / "config" / "config.yaml").read_text()) or {}
    for part in path.split("."):
        data = data[part]
    print(data)
except Exception:
    print(default)
PYEOF
}

probe() {  # probe [--quote-check] -> key=value lines on stdout
  ( cd "$SCANNER_DIR" && "$PY" scripts/ib_probe.py \
      --host "$(scanner_cfg broker.host 127.0.0.1)" \
      --port "$(scanner_cfg broker.port 4002)" \
      --client-id "$PROBE_CLIENT_ID" "$@" 2>/dev/null )
}

field() { grep -m1 "^$1=" <<<"$2" | cut -d= -f2-; }

# Both bots run under a supervisor whose own command line contains the module
# name, so a bare pgrep -f matches the wrapper as well as the interpreter. Pick
# the python process, or the uptime and the patch-age check describe the wrapper.
# macOS pgrep has no -a; -l is what lists the command line.
_py_pid() { pgrep -fl "$1" | awk 'tolower($2) ~ /python/ {print $1; exit}'; }
scanner_pid() { _py_pid "[o]ptions_scanner.main"; }
warrior_pid() { _py_pid "[w]arrior_bot.main"; }
gateway_pid() { pgrep -f "IB Gateway" | head -1; }
uptime_of()   { [[ -n "${1:-}" ]] && ps -o etime= -p "$1" 2>/dev/null | tr -d ' '; }

# --- status ---------------------------------------------------------------

cmd_status() {
  local port; port="$(scanner_cfg broker.port 4002)"
  head_ "Processes"
  local gp wp sp
  gp="$(gateway_pid)"; wp="$(warrior_pid)"; sp="$(scanner_pid)"
  if [[ -n "$gp" ]] && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    printf '  gateway   %sUP%s    :%s  pid %s  up %s\n' "$G" "$N" "$port" "$gp" "$(uptime_of "$gp")"
  else
    printf '  gateway   %sDOWN%s  nothing listening on :%s\n' "$R" "$N" "$port"
  fi
  if [[ -n "$wp" ]]; then
    printf '  warrior   %sUP%s    client %s  pid %s  up %s\n' "$G" "$N" \
      "$(warrior_cfg trading.client_id '?')" "$wp" "$(uptime_of "$wp")"
  else
    printf '  warrior   %sDOWN%s\n' "$R" "$N"
  fi
  if [[ -n "$sp" ]]; then
    printf '  scanner   %sUP%s    client %s  pid %s  up %s\n' "$G" "$N" \
      "$(scanner_cfg broker.client_id '?')" "$sp" "$(uptime_of "$sp")"
  else
    printf '  scanner   %sDOWN%s\n' "$R" "$N"
  fi

  head_ "Session"
  ( cd "$SCANNER_DIR" && "$PY" -c "
from options_scanner.market_hours import describe
print('  ' + describe())" 2>/dev/null ) || echo "  (could not read the calendar)"
  local sa wa
  sa="$(scanner_cfg broker.account '')"; wa="$(warrior_cfg trading.account '')"
  printf '  scanner mode=%s broker=%s account=%s\n' \
    "$(scanner_cfg mode '?')" "$(scanner_cfg broker.kind '?')" "${sa:-(the only one)}"
  printf '  warrior account=%s\n' "${wa:-(the only one)}"

  head_ "Account"
  if [[ -z "$gp" ]]; then
    echo "  (gateway down — nothing to ask)"
  else
    local out; out="$(probe)"
    if [[ "$(field connected "$out")" == "yes" ]]; then
      printf '  %s  paper=%s  net_liq=%s\n' \
        "$(field account "$out")" "$(field is_paper "$out")" "$(field net_liquidation "$out")"
      # One line per managed account, and which account holds what, so the
      # isolation can be seen rather than assumed once the bots are split.
      grep -E '^(account_[0-9]+|positions_in_)' <<<"$out" | while IFS='=' read -r key value; do
        case "$key" in
          account_*)      printf '    managed: %s\n' "$value" ;;
          positions_in_*) printf '    holds %s position(s): %s\n' "$value" "${key#positions_in_}" ;;
        esac
      done
      printf '  positions: %s (%s opt / %s stk)   open orders: %s   from clients: %s\n' \
        "$(field positions_total "$out")" "$(field positions_opt "$out")" \
        "$(field positions_stk "$out")" "$(field open_orders "$out")" \
        "$(field client_ids_with_orders "$out")"
    else
      printf '  %sconnect failed%s: %s\n' "$R" "$N" "$(field connect_error "$out")"
    fi
  fi

  head_ "Open positions the scanner is managing"
  ( cd "$SCANNER_DIR" && "$PY" -c "
from options_scanner.config import load_settings
from options_scanner.storage import Storage
s = load_settings()
st = Storage(s.resolve_path(s.storage.db_path))
rows = st.open_positions()
if not rows:
    print('  none')
for _, p in rows:
    stop = f'\${p.stop_price:.2f}' if p.stop_price else 'NONE'
    print(f'  {p.option.occ_symbol}  {p.remaining_qty}/{p.original_qty}  '
          f'entry \${p.entry_fill:.3f}  peak \${p.peak_bid:.3f}  stop {stop}  '
          f'trims {sorted(p.fired_levels) or \"[]\"}')
st.close()" 2>/dev/null ) || echo "  (could not read the database — .env may be incomplete)"
  echo
}

# --- doctor ---------------------------------------------------------------

cmd_doctor() {
  local port mode kind sclient wclient
  port="$(scanner_cfg broker.port 4002)"
  mode="$(scanner_cfg mode '?')"
  kind="$(scanner_cfg broker.kind '?')"
  sclient="$(scanner_cfg broker.client_id '?')"
  wclient="$(warrior_cfg trading.client_id '?')"

  head_ "Gateway"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    ok "listening on :$port (pid $(gateway_pid), up $(uptime_of "$(gateway_pid)"))"
  else
    bad "nothing listening on :$port — start IB Gateway and log in"
  fi
  if [[ "$port" == "4001" || "$port" == "7496" ]] && [[ "$mode" != "live" ]]; then
    bad "port $port is a LIVE IBKR port but mode is '$mode'"
  else
    ok "port $port is consistent with mode '$mode'"
  fi
  if grep -q "Daily auto-restart is not enabled" "$HOME/Jts/launcher.log" 2>/dev/null; then
    warn "Gateway auto-restart is NOT enabled — nothing brings it back when it drops"
    note "Configure -> Settings -> Lock and Exit -> Auto restart (not Auto logoff)"
  fi
  note "Master API client ID must be BLANK (Configure -> Settings -> API) — check by eye"

  head_ "Client IDs"
  if [[ "$sclient" == "$wclient" ]]; then
    bad "both bots want client id $sclient — IBKR refuses the second connection"
  else
    ok "scanner=$sclient warrior=$wclient probe=$PROBE_CLIENT_ID (all distinct)"
  fi
  # Client 0 is special at IBKR: it additionally receives orders placed by hand
  # in TWS, which breaks the assumption that openTrades() only contains orders
  # the bot itself placed -- the basis of warrior_bot's stop-coverage scan.
  for pair in "scanner:$sclient" "warrior:$wclient"; do
    [[ "${pair#*:}" == "0" ]] && bad "${pair%%:*} is on client id 0, which also receives manual TWS orders"
  done

  head_ "Account isolation"
  local saccount waccount managed
  saccount="$(scanner_cfg broker.account '')"
  waccount="$(warrior_cfg trading.account '')"
  managed=""
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    managed="$(field account "$(probe)")"
  fi

  if [[ -z "$saccount" && -z "$waccount" ]]; then
    # One account under the login is the shared-paper setup: IBKR allows one
    # paper account per login, so both bots trade it. Blank is correct there.
    # It stops being correct the moment a second account appears.
    if [[ -z "$managed" ]]; then
      # Without the gateway there is nothing to ask. Saying "ok" here would
      # vouch for the one check most likely to stop a bot from starting.
      warn "could not check how many accounts this login manages — gateway is down"
    elif [[ "$managed" == *,* ]]; then
      bad "this login manages several accounts ($managed) but neither bot names one"
      note "set broker.account (config.yaml) and trading.account (warrior config/config.yaml)"
    else
      ok "both bots share $managed — the stock-only guard below is what keeps them apart"
    fi
  elif [[ "$saccount" == "$waccount" ]]; then
    # Naming the same account in both is the shared-paper setup made explicit.
    ok "both bots name $saccount — shared; the stock-only guard below keeps them apart"
    if [[ -n "$managed" && ",$managed," != *",$saccount,"* ]]; then
      bad "$saccount is not managed by this login ($managed)"
    fi
  else
    ok "scanner=${saccount:-(unset)} warrior=${waccount:-(unset)} are different"
    for pair in "scanner:$saccount" "warrior:$waccount"; do
      local who="${pair%%:*}" acct="${pair#*:}"
      [[ -z "$acct" ]] && { warn "$who names no account while the other does"; continue; }
      if [[ -n "$managed" && ",$managed," != *",$acct,"* ]]; then
        bad "$who is configured for $acct, which this login does not manage ($managed)"
      fi
    done
  fi

  # reqGlobalCancel takes no account argument, so it cancels across accounts.
  # Match the call form (ib.reqGlobalCancel) rather than the bare name, which
  # also appears in the docstring explaining why it was removed.
  if grep -qE "\bib\.reqGlobalCancel\(" "$WARRIOR_DIR/warrior_bot/utils/panic.py" 2>/dev/null; then
    bad "warrior_bot still calls reqGlobalCancel() — it cannot be scoped and will cancel our orders"
  else
    ok "warrior_bot's panic path cancels per-order, not globally"
  fi

  head_ "warrior_bot interference guard (shared paper account)"
  # IBKR allows one paper account per login, so warrior_bot shares ours.
  # Account scoping cannot separate the bots there; security type does. Each
  # of these files must ignore anything that is not a stock, or warrior_bot
  # acts on our option positions.
  local -a guard_files=(
    'warrior_bot/main.py|secType == "STK"|its reconciliation watchdog flattens our options within 30s'
    'warrior_bot/utils/panic.py|is_stock(p.contract)|its 15:55 EOD flatten closes our options every day'
    'warrior_bot/risk/account_state.py|is_stock(|our options count against its risk limits and slots'
  )
  local newest=0 entry rel needle consequence path m
  for entry in "${guard_files[@]}"; do
    IFS='|' read -r rel needle consequence <<<"$entry"
    path="$WARRIOR_DIR/$rel"
    if grep -qF "$needle" "$path" 2>/dev/null; then
      ok "${rel##*/}: stock-only filter present"
      m="$(stat -f %m "$path")"
      (( m > newest )) && newest=$m
    else
      bad "${rel##*/}: stock-only filter MISSING — $consequence"
    fi
    if ! git -C "$WARRIOR_DIR" show "main:$rel" 2>/dev/null | grep -qF "$needle"; then
      warn "${rel##*/}: the filter is not on warrior_bot's main — a checkout would undo it"
    fi
  done

  # A process started before the newest of those files is still running the
  # old code, whatever the working tree says.
  local wp; wp="$(warrior_pid)"
  if [[ -z "$wp" ]]; then
    warn "warrior_bot is not running, so nothing to verify"
  elif (( newest > 0 )); then
    local started
    started="$(date -j -f "%a %b %d %T %Y" "$(ps -o lstart= -p "$wp" | sed 's/ *$//')" +%s 2>/dev/null || echo 0)"
    if (( started < newest )); then
      bad "warrior_bot's RUNNING process predates the stock-only patch — restart it"
      note "kill the Python child only (not SCREEN); its supervisor relaunches it on current code"
    else
      ok "warrior_bot's running process is newer than the stock-only patch"
    fi
  fi

  head_ "options-scanner config"
  local missing=()
  for key in DISCORD_BOT_TOKEN OWNER_USER_ID ALERTS_CHANNEL_ID UPDATES_CHANNEL_ID; do
    local value; value="$(grep -m1 "^$key=" "$SCANNER_DIR/.env" 2>/dev/null | cut -d= -f2-)"
    [[ -z "$value" ]] && missing+=("$key")
  done
  if ((${#missing[@]})); then
    bad ".env is missing: ${missing[*]}"
  else
    ok ".env has every required key"
  fi
  if ( cd "$SCANNER_DIR" && "$PY" -m options_scanner.main --check >/dev/null 2>&1 ); then
    ok "config loads and validates (mode=$mode broker=$kind)"
  else
    bad "config does not load — run: $PY -m options_scanner.main --check"
  fi

  head_ "Market data (decides whether stops can work at all)"
  if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    warn "skipped — gateway is down"
  else
    local out; out="$(probe --quote-check)"
    case "$(field quote_check "$out")" in
      ok) ok "option quotes arrive (bid $(field option_bid "$out") on $(field quote_contract "$out"))" ;;
      failed)
        bad "no option bid: $(field quote_check_detail "$out")"
        note "without a bid the trim ladder and the synthetic stop never fire"
        note "fix: Client Portal -> add OPRA (NP,L1), then share market data with the paper account" ;;
      *) warn "could not run the quote check (gateway reachable?)" ;;
    esac
    [[ "$(field is_paper "$out")" == "no" ]] && bad "account $(field account "$out") is NOT a paper account"
  fi

  head_ "Machine"
  local sleep_ac
  sleep_ac="$(pmset -g custom 2>/dev/null | sed -n '/AC Power/,/^$/p' | awk '/^ *sleep/{print $2; exit}')"
  if [[ "$sleep_ac" == "0" ]]; then
    ok "the Mac never sleeps on AC power"
  else
    bad "pmset sleep=$sleep_ac on AC — a sleeping Mac has no synthetic stop"
    note "fix: sudo pmset -c sleep 0 disksleep 0"
  fi
  if pgrep -x caffeinate >/dev/null; then
    warn "relying on a caffeinate process; it is unsupervised and dies silently"
  fi

  head_ "launchd agent"
  if [[ -f "$PLIST" ]]; then
    local line; line="$(launchctl list 2>/dev/null | grep -F "$LABEL")"
    if [[ -z "$line" ]]; then
      warn "plist exists but the agent is not loaded (./ops.sh start scanner)"
    else
      local last; last="$(awk '{print $2}' <<<"$line")"
      if [[ "$last" == "0" || "$last" == "-" ]]; then
        ok "agent loaded, last exit $last"
      else
        bad "agent is crash-looping (last exit $last) — see ./ops.sh logs scanner"
      fi
    fi
  else
    warn "no launchd agent installed yet"
  fi

  head_ "Market-data line budget"
  local cap; cap="$(warrior_cfg data_watchdog.max_concurrent_subscriptions 90)"
  local slots; slots="$(scanner_cfg risk.max_open_positions 3)"
  if (( cap + slots > 100 )); then
    warn "warrior caps at $cap subscriptions + scanner needs up to $slots; IBKR's default is 100"
  else
    ok "$cap + up to $slots subscriptions, within IBKR's default 100"
  fi

  printf '\n%s%d failed, %d warnings%s\n\n' "$B" "$FAILS" "$WARNS" "$N"
  (( FAILS == 0 ))
}

# --- lifecycle ------------------------------------------------------------

scanner_start() {
  [[ -f "$PLIST" ]] || { bad "no plist at $PLIST"; return 1; }
  launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null \
    || launchctl load -w "$PLIST" 2>/dev/null
  sleep 2; [[ -n "$(scanner_pid)" ]] && ok "scanner started (pid $(scanner_pid))" || warn "scanner did not come up — ./ops.sh logs scanner"
}

scanner_stop() {
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null \
    || launchctl unload -w "$PLIST" 2>/dev/null
  sleep 1; [[ -z "$(scanner_pid)" ]] && ok "scanner stopped" || warn "scanner still running"
}

warrior_restart() {
  local wp; wp="$(warrior_pid)"
  if [[ -z "$wp" ]]; then
    warn "warrior_bot is not running; its supervisor loop lives in a login shell and cannot be started from here"
    return 1
  fi
  # Its `until` loop restarts the python within ~30s, so killing the child is
  # the restart. Killing the loop would need the login shell to recreate it.
  kill "$wp" && ok "signalled warrior_bot (pid $wp); its supervisor restarts it within ~30s"
  note "verify with: ./scripts/ops.sh status"
}

cmd_logs() {
  case "${1:-scanner}" in
    scanner) tail -f "$SCANNER_DIR/logs/options_scanner.log" ;;
    launchd) tail -f "$SCANNER_DIR/logs/launchd.err" ;;
    warrior) tail -f "$WARRIOR_DIR/data/warrior_bot.log" ;;
    gateway) tail -f "$HOME/Jts/launcher.log" ;;
    *) echo "logs [scanner|launchd|warrior|gateway]"; return 1 ;;
  esac
}

case "${1:-status}" in
  status) cmd_status ;;
  doctor) cmd_doctor ;;
  start)  case "${2:-all}" in scanner|all) scanner_start ;; warrior) warn "warrior_bot starts from its own login shell" ;; esac ;;
  stop)   case "${2:-all}" in scanner|all) scanner_stop ;; warrior) warn "use 'restart warrior'; stopping it needs the login shell" ;; esac ;;
  restart)
    case "${2:-all}" in
      scanner) scanner_stop; scanner_start ;;
      warrior) warrior_restart ;;
      all) warrior_restart; scanner_stop; scanner_start ;;
    esac ;;
  logs)   cmd_logs "${2:-scanner}" ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ; exit 1 ;;
esac
