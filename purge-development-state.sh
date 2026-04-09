#!/usr/bin/env sh
set -eu

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
CONFIG_PATH="${CONFIG_PATH:-$OPENCLAW_HOME/openclaw.json}"

DRY_RUN=0
ASSUME_YES=0

GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-}"

PURGE_DIR_NAMES='
sessions
events
artifacts
projects
archives
memory
memories
logs
snapshots
generated
specs
node_modules
dist
build
.pytest_cache
.openclaw
'

DELETE_FILE_SUFFIXES='
.jsonl
.lock
.log
'

usage() {
  cat <<'USAGE'
Usage:
  purge-development-state.sh --yes [--dry-run]

What it does:
  1. Stop OpenClaw gateway and wait until its listener is fully gone
  2. Parse ~/.openclaw/openclaw.json and discover every configured agent dynamically
  3. Purge each configured agent's runtime state
  4. Purge inferred team/shared runtime state
  5. Purge transient runtime workspaces/tasks created while agents were running
  6. Stabilize runtime on disk before the gateway can see stale work again
  7. Start OpenClaw gateway
  8. Check gateway status

What it clears by default:
  - bot-authored Feishu group messages referenced by persisted status history
  - all configured agent session transcripts (*.jsonl)
  - all configured agent session indexes (sessions.json)
  - per-agent memories/runtime directories when present
  - workspace runtime state and historical output directories such as:
      sessions/, events/, artifacts/, projects/, archives/,
      memory/, memories/, logs/, snapshots/, generated/, specs/
  - workspace runtime JSON such as:
      task.json, status.json, handoff.json, workflow.json, team-status.json
  - inferred team shared state such as _shared/development-ports.json
  - shared runtime queues under ~/.openclaw:
      tasks/, flows/, subagents/, delivery-queue/,
      memory/, memories/, logs/, exec-approvals.json
  - transient runtime workspaces such as workspace-gateway-* and agents/mc-gateway-*

What it preserves:
  - openclaw.json and channel/model configuration
  - agent skeleton/config files under agentDir
  - transport dedup/checkpoint state such as feishu/dedup to avoid replaying old inbound messages
  - workspace/framework files that are not recognized as runtime/history/output state

Environment:
  OPENCLAW_HOME           Override OpenClaw home (default: ~/.openclaw)
  CONFIG_PATH             Override config path (default: $OPENCLAW_HOME/openclaw.json)
USAGE
}

log() {
  printf '%s\n' "$*"
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run]'
    for arg in "$@"; do
      printf ' %s' "$arg"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

run_soft() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run][soft]'
    for arg in "$@"; do
      printf ' %s' "$arg"
    done
    printf '\n'
    return 0
  fi
  if "$@"; then
    return 0
  fi
  log "Non-fatal command failed: $*"
  return 0
}

write_json() {
  path="$1"
  content="$2"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] write %s\n' "$path"
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" > "$path"
}

recall_feishu_group_messages() {
  python3 - "$CONFIG_PATH" "$WORKSPACES_FILE" "$OPENCLAW_HOME" "$DRY_RUN" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

config_path = Path(sys.argv[1])
workspaces_file = Path(sys.argv[2])
openclaw_home = Path(sys.argv[3])
dry_run = sys.argv[4] == "1"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def resolve_secret(value, env_map: dict[str, str]) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) and value.get("source") == "env":
        secret_id = value.get("id")
        if isinstance(secret_id, str):
            return env_map.get(secret_id, "").strip()
    return ""


def resolve_domain_base(domain_value) -> str:
    domain = (domain_value or "feishu").strip()
    if domain in {"", "feishu"}:
        return "https://open.feishu.cn"
    if domain == "lark":
        return "https://open.larksuite.com"
    if domain.startswith("http://") or domain.startswith("https://"):
        return domain.rstrip("/")
    return f"https://{domain.rstrip('/')}"


cfg = load_json(config_path, {})
feishu_cfg = ((cfg.get("channels") or {}).get("feishu") or {})
accounts = feishu_cfg.get("accounts") or {}
if not isinstance(accounts, dict) or not accounts:
    sys.exit(0)

env_map = dict(os.environ)
env_map.update(load_dotenv(openclaw_home / ".env"))

message_refs: dict[str, dict[str, str]] = {}
for raw_workspace in workspaces_file.read_text(encoding="utf-8").splitlines():
    workspace = raw_workspace.strip()
    if not workspace:
        continue
    status = load_json(Path(workspace) / "status.json", {})
    if not isinstance(status, dict):
        continue
    account_id = str(status.get("account_id") or status.get("agent_id") or "").strip()
    if not account_id:
        continue

    candidates = []
    history = status.get("group_reply_history")
    if isinstance(history, list):
        candidates.extend(entry for entry in history if isinstance(entry, dict))
    last_reply = status.get("last_group_reply")
    if isinstance(last_reply, dict):
        candidates.append(last_reply)

    for entry in candidates:
        message_id = str(entry.get("message_id") or "").strip()
        if not message_id:
            continue
        if message_id in message_refs:
            continue
        message_refs[message_id] = {
            "account_id": account_id,
            "kind": str(entry.get("kind") or "").strip(),
            "target": str(entry.get("target") or "").strip(),
        }

if not message_refs:
    sys.exit(0)

if dry_run:
    for message_id, meta in sorted(message_refs.items()):
        print(f"[dry-run] recall feishu message {message_id} via {meta['account_id']} ({meta['kind']})")
    sys.exit(0)

tokens: dict[str, tuple[str, str]] = {}
for account_id in sorted({meta["account_id"] for meta in message_refs.values()}):
    account_cfg = accounts.get(account_id) or {}
    if not isinstance(account_cfg, dict):
        print(f"[warn] skip Feishu recall for {account_id}: account config missing", file=sys.stderr)
        continue
    app_id = str(account_cfg.get("appId") or feishu_cfg.get("appId") or "").strip()
    app_secret = resolve_secret(account_cfg.get("appSecret") or feishu_cfg.get("appSecret"), env_map)
    domain_base = resolve_domain_base(account_cfg.get("domain") or feishu_cfg.get("domain"))
    if not app_id or not app_secret:
        print(f"[warn] skip Feishu recall for {account_id}: missing app credentials", file=sys.stderr)
        continue
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        f"{domain_base}/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[warn] skip Feishu recall for {account_id}: token request failed ({exc})", file=sys.stderr)
        continue
    if payload.get("code") != 0 or not payload.get("tenant_access_token"):
        print(
            f"[warn] skip Feishu recall for {account_id}: token request rejected ({payload.get('msg') or payload.get('message') or payload.get('code')})",
            file=sys.stderr,
        )
        continue
    tokens[account_id] = (domain_base, str(payload["tenant_access_token"]))

for message_id, meta in sorted(message_refs.items()):
    account_id = meta["account_id"]
    token_info = tokens.get(account_id)
    if token_info is None:
        continue
    domain_base, access_token = token_info
    url = f"{domain_base}/open-apis/im/v1/messages/{urllib.parse.quote(message_id, safe='')}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {"code": exc.code, "msg": str(exc)}
    except Exception as exc:
        print(f"[warn] failed to recall Feishu message {message_id}: {exc}", file=sys.stderr)
        continue

    if payload.get("code") == 0:
        print(f"[info] recalled Feishu message {message_id} via {account_id}")
    else:
        print(
            f"[warn] failed to recall Feishu message {message_id} via {account_id}: {payload.get('msg') or payload.get('message') or payload.get('code')}",
            file=sys.stderr,
        )
PY
}

remove_path() {
  path="$1"
  if [ ! -e "$path" ]; then
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] rm -rf %s\n' "$path"
    return 0
  fi
  rm -rf "$path"
}

remove_dir_contents() {
  dir="$1"
  if [ ! -d "$dir" ]; then
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    find "$dir" -mindepth 1 -maxdepth 1 -print | sed 's#^#[dry-run] rm -rf #' || true
    return 0
  fi
  find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
}

newline_list_has() {
  needle="$1"
  haystack="$2"
  case "
$haystack
" in
    *"
$needle
"*) return 0 ;;
    *) return 1 ;;
  esac
}

path_is_vcs() {
  case "$1" in
    */.git|*/.git/*|*/.hg|*/.hg/*|*/.svn|*/.svn/*) return 0 ;;
    *) return 1 ;;
  esac
}

path_has_suffix() {
  path="$1"
  suffixes="$2"
  while IFS= read -r suffix; do
    [ -n "$suffix" ] || continue
    case "$path" in
      *"$suffix") return 0 ;;
    esac
  done <<EOF
$suffixes
EOF
  return 1
}

reset_agent_state_files() {
  agent_root="$1"

  if [ -f "$agent_root/sessions.json" ]; then
    write_json "$agent_root/sessions.json" '{}'
  fi
  if [ -f "$agent_root/sessions/sessions.json" ]; then
    write_json "$agent_root/sessions/sessions.json" '{}'
  fi
}

reset_workspace_state_files() {
  workspace_root="$1"

  if [ -f "$workspace_root/task.json" ]; then
    write_json "$workspace_root/task.json" '{"task_id":null,"title":null,"status":"idle","current_stage":null}'
  fi
  if [ -f "$workspace_root/status.json" ]; then
    write_json "$workspace_root/status.json" '{"status":"idle","current_stage":null,"last_group_reply":null}'
  fi
  if [ -f "$workspace_root/handoff.json" ]; then
    write_json "$workspace_root/handoff.json" '{"project_name":null,"from":null,"to":null,"artifacts":[],"notes":null}'
  fi
  if [ -f "$workspace_root/workflow.json" ]; then
    write_json "$workspace_root/workflow.json" '{"project_name":null,"current_phase":null,"active_task_id":null,"phases":[],"blocked":false,"block_reason":null}'
  fi
  if [ -f "$workspace_root/team-status.json" ]; then
    write_json "$workspace_root/team-status.json" '{"project_name":null,"phase":null,"agents":{}}'
  fi
  if [ -f "$workspace_root/development-ports.json" ]; then
    write_json "$workspace_root/development-ports.json" '{"shared":[],"projects":{}}'
  fi
  if [ -f "$workspace_root/_shared/development-ports.json" ]; then
    write_json "$workspace_root/_shared/development-ports.json" '{"shared":[],"projects":{}}'
  fi
  if [ -f "$workspace_root/group_sequence.json" ]; then
    remove_path "$workspace_root/group_sequence.json"
  fi
  if [ -f "$workspace_root/watchdog.json" ]; then
    remove_path "$workspace_root/watchdog.json"
  fi
  if [ -f "$workspace_root/.last-run.json" ]; then
    remove_path "$workspace_root/.last-run.json"
  fi
  if [ -f "$workspace_root/.openclaw/workspace-state.json" ]; then
    remove_path "$workspace_root/.openclaw/workspace-state.json"
  fi
}

purge_named_dirs_under() {
  root="$1"
  if [ ! -d "$root" ]; then
    return 0
  fi

  find "$root" -depth -type d -print 2>/dev/null | sort -r | while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    if path_is_vcs "$dir"; then
      continue
    fi
    base="$(basename "$dir")"
    if newline_list_has "$base" "$PURGE_DIR_NAMES"; then
      remove_dir_contents "$dir"
    fi
  done
}

purge_runtime_files_under() {
  root="$1"
  if [ ! -d "$root" ]; then
    return 0
  fi

  find "$root" -type f -print 2>/dev/null | while IFS= read -r file; do
    [ -n "$file" ] || continue
    if path_is_vcs "$file"; then
      continue
    fi
    case "$file" in
      */sessions/*.jsonl|*/sessions/*.jsonl.reset.*|*.jsonl|*.jsonl.reset.*)
        remove_path "$file"
        continue
        ;;
    esac
    if path_has_suffix "$file" "$DELETE_FILE_SUFFIXES"; then
      remove_path "$file"
    fi
  done
}

trim_agent_runtime_sessions() {
  agent_root="$1"
  sessions_dir="$agent_root/sessions"
  sessions_index="$sessions_dir/sessions.json"

  if [ ! -d "$sessions_dir" ]; then
    return 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] trim runtime sessions under %s\n' "$sessions_dir"
    return 0
  fi

  python3 - "$sessions_index" "$sessions_dir" <<'PY'
import json
import re
import sys
from pathlib import Path

index_path = Path(sys.argv[1])
sessions_dir = Path(sys.argv[2])
allowed = re.compile(r"^agent:[^:]+:heartbeat-control$")

data = {}
if index_path.exists():
    try:
        with index_path.open("r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}

kept = {}
referenced = set()
for key, value in data.items():
    if not allowed.fullmatch(str(key)):
        continue
    if isinstance(value, dict):
        kept[key] = value
        session_file = value.get("sessionFile")
        if isinstance(session_file, str) and session_file:
            referenced.add(str(Path(session_file).resolve()))

for path in sessions_dir.glob("*.jsonl"):
    if str(path.resolve()) not in referenced:
        path.unlink(missing_ok=True)

index_path.parent.mkdir(parents=True, exist_ok=True)
with index_path.open("w", encoding="utf-8") as fh:
    json.dump(kept, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
}

stabilize_runtime_after_restart() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log '[dry-run] stabilize runtime after restart'
    return 0
  fi

  sleep 2

  while IFS= read -r agent_root; do
    [ -n "$agent_root" ] || continue
    trim_agent_runtime_sessions "$agent_root"
    reset_agent_state_files "$agent_root"
  done < "$AGENT_ROOTS_FILE"

  while IFS= read -r workspace_root; do
    [ -n "$workspace_root" ] || continue
    reset_workspace_state_files "$workspace_root"
    prune_workspace_outputs "$workspace_root" "$AGENT_IDS_FILE" "$WORKSPACES_FILE"
  done < "$WORKSPACES_FILE"

  while IFS= read -r team_root; do
    [ -n "$team_root" ] || continue
    reset_workspace_state_files "$team_root"
  done < "$TEAM_ROOTS_FILE"
}

verify_no_resumed_work() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log '[dry-run] verify no resumed work'
    return 0
  fi

  python3 - "$AGENT_ROOTS_FILE" "$WORKSPACES_FILE" "$TEAM_ROOTS_FILE" "$OPENCLAW_HOME" <<'PY'
import json
import re
import sys
from pathlib import Path

agent_roots_file = Path(sys.argv[1])
workspaces_file = Path(sys.argv[2])
team_roots_file = Path(sys.argv[3])
openclaw_home = Path(sys.argv[4])

allowed_session = re.compile(r"^agent:[^:]+:heartbeat-control$")
problems = []

def read_lines(path: Path):
    if not path.exists():
        return []
    return [Path(line.strip()) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

for agent_root in read_lines(agent_roots_file):
    sessions_index = agent_root / "sessions" / "sessions.json"
    if not sessions_index.exists():
        continue
    try:
        data = json.loads(sessions_index.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"invalid session index: {sessions_index} ({exc})")
        continue
    if not isinstance(data, dict):
        problems.append(f"invalid session index shape: {sessions_index}")
        continue
    for key in data.keys():
        if not allowed_session.fullmatch(str(key)):
            problems.append(f"unexpected active session: {sessions_index} -> {key}")

runtime_dir_names = [
    "projects",
    "archives",
    "events",
    "artifacts",
    "memory",
    "memories",
    "logs",
    "snapshots",
    "generated",
    "specs",
]

state_file_names = [
    "group_sequence.json",
    "watchdog.json",
    ".last-run.json",
]

for root in read_lines(workspaces_file) + read_lines(team_roots_file):
    for name in runtime_dir_names:
        path = root / name
        if path.is_dir():
            try:
                next(path.iterdir())
            except StopIteration:
                pass
            else:
                problems.append(f"non-empty runtime dir after restart: {path}")
    for name in state_file_names:
        path = root / name
        if path.exists():
            problems.append(f"unexpected runtime marker after restart: {path}")

for name in ["tasks", "flows", "subagents", "delivery-queue", "memory", "memories"]:
    path = openclaw_home / name
    if path.is_dir():
        try:
            next(path.iterdir())
        except StopIteration:
            pass
        else:
            problems.append(f"non-empty shared runtime dir after restart: {path}")

if problems:
    for item in problems:
        print(item)
    sys.exit(1)
PY
}

purge_root() {
  label="$1"
  root="$2"
  if [ ! -e "$root" ]; then
    return 0
  fi
  log "Purging $label: $root"
  if [ -d "$root" ]; then
    purge_named_dirs_under "$root"
    purge_runtime_files_under "$root"
  else
    remove_path "$root"
  fi
}

path_in_file_exact() {
  file="$1"
  value="$2"
  grep -F -x "$value" "$file" >/dev/null 2>&1
}

prune_workspace_outputs() {
  workspace_root="$1"
  agent_ids_file="$2"
  workspace_roots_file="$3"

  if [ ! -d "$workspace_root" ]; then
    return 0
  fi

  find "$workspace_root" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | while IFS= read -r child; do
    [ -n "$child" ] || continue
    if path_is_vcs "$child"; then
      continue
    fi

    base="$(basename "$child")"
    case "$base" in
      .openclaw|_shared|shared-api-docs|projects|archives|sessions|events|artifacts|memory|memories|logs|snapshots|generated|specs|node_modules|dist|build|.pytest_cache)
        continue
        ;;
    esac

    if [ "$base" = "agents" ]; then
      remove_path "$child"
      continue
    fi

    if path_in_file_exact "$workspace_roots_file" "$child"; then
      continue
    fi

    if [ -z "$(find "$child" -mindepth 1 -print -quit 2>/dev/null)" ]; then
      remove_path "$child"
      continue
    fi

    if path_in_file_exact "$agent_ids_file" "$base"; then
      remove_path "$child"
    fi
  done
}

emit_purge_plan() {
  python3 - "$CONFIG_PATH" "$OPENCLAW_HOME" <<'PY'
import json
import os
import sys
from pathlib import Path

config_path = Path(os.path.expanduser(sys.argv[1])).resolve()
openclaw_home = Path(os.path.expanduser(sys.argv[2])).resolve()

with config_path.open("r", encoding="utf-8") as fh:
    config = json.load(fh)

agents = config.get("agents", {})
defaults = agents.get("defaults", {})
default_workspace = defaults.get("workspace") or str(openclaw_home / "workspace")
agent_list = agents.get("list", [])

records = []
agent_ids: set[str] = set()
for entry in agent_list:
    if not isinstance(entry, dict):
        continue
    agent_id = entry.get("id")
    if not isinstance(agent_id, str) or not agent_id:
        continue
    agent_ids.add(agent_id)
    workspace_raw = entry.get("workspace") or default_workspace
    agent_dir_raw = entry.get("agentDir") or str(openclaw_home / "agents" / agent_id / "agent")
    workspace = Path(os.path.expanduser(str(workspace_raw))).resolve()
    agent_dir = Path(os.path.expanduser(str(agent_dir_raw))).resolve()
    records.append(
        (
            agent_id,
            str(workspace),
            str(agent_dir),
            str(agent_dir.parent),
        )
    )

parent_map: dict[str, set[str]] = {}
for agent_id, workspace, _agent_dir, _agent_root in records:
    workspace_path = Path(workspace)
    if workspace_path.name != agent_id:
        continue
    parent_map.setdefault(str(workspace_path.parent), set()).add(agent_id)

team_roots = sorted(parent for parent, names in parent_map.items() if len(names) >= 2)

for agent_id, workspace, agent_dir, agent_root in records:
    print(f"AGENT\t{agent_id}\t{workspace}\t{agent_dir}\t{agent_root}")

for team_root in team_roots:
    print(f"TEAM\t{team_root}")

for transient in sorted(openclaw_home.glob("workspace-gateway-*")):
    print(f"TRANSIENT\t{transient.resolve()}")

agents_root = openclaw_home / "agents"
if agents_root.exists():
    for transient in sorted(agents_root.glob("mc-gateway-*")):
        print(f"TRANSIENT\t{transient.resolve()}")
PY
}

stop_openclaw_services() {
  log 'Stopping OpenClaw gateway'
  run_soft openclaw gateway stop
  wait_for_gateway_shutdown
}

check_openclaw_gateway_status() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log '[dry-run] openclaw gateway status'
    return 0
  fi

  gateway_attempts=10
  gateway_try=1
  while [ "$gateway_try" -le "$gateway_attempts" ]; do
    if openclaw gateway status; then
      return 0
    fi
    if [ "$gateway_try" -eq "$gateway_attempts" ]; then
      return 1
    fi
    sleep 2
    gateway_try=$((gateway_try + 1))
  done

  return 1
}

start_openclaw_gateway() {
  log 'Starting OpenClaw gateway'
  run_soft openclaw gateway start
}

resolve_gateway_port() {
  if [ -n "${GATEWAY_PORT:-}" ]; then
    printf '%s\n' "$GATEWAY_PORT"
    return 0
  fi

  GATEWAY_PORT="$(python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
port = 18789
try:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    gateway = data.get("gateway") or {}
    value = gateway.get("port")
    if isinstance(value, int) and value > 0:
        port = value
except Exception:
    pass

print(port)
PY
)"
  printf '%s\n' "$GATEWAY_PORT"
}

gateway_listener_pids() {
  port="$(resolve_gateway_port)"
  lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
}

wait_for_gateway_shutdown() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log '[dry-run] wait for gateway shutdown'
    return 0
  fi

  attempts=15
  try=1
  while [ "$try" -le "$attempts" ]; do
    pids="$(gateway_listener_pids || true)"
    if [ -z "$pids" ]; then
      return 0
    fi
    sleep 1
    try=$((try + 1))
  done

  pids="$(gateway_listener_pids || true)"
  if [ -z "$pids" ]; then
    return 0
  fi

  log "Gateway listener still alive after graceful stop; terminating lingering PIDs: $(printf '%s ' $pids)"
  kill $pids 2>/dev/null || true
  sleep 2

  pids="$(gateway_listener_pids || true)"
  if [ -z "$pids" ]; then
    return 0
  fi

  log "Gateway listener still alive after SIGTERM; forcing kill: $(printf '%s ' $pids)"
  kill -9 $pids 2>/dev/null || true
  sleep 1

  pids="$(gateway_listener_pids || true)"
  if [ -n "$pids" ]; then
    printf 'Failed to stop gateway listener on port %s (PIDs: %s)\n' "$(resolve_gateway_port)" "$(printf '%s ' $pids)" >&2
    exit 1
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|--force)
      ASSUME_YES=1
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --purge-shared-memory|--purge-shared-runtime)
      log "Flag $1 is now implicit and no longer needed."
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Refusing to run without --yes. This script permanently deletes agent runtime state.\n' >&2
  exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
  printf 'Config not found: %s\n' "$CONFIG_PATH" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  printf 'python3 is required for config parsing.\n' >&2
  exit 1
fi

PLAN_FILE="$(mktemp)"
AGENT_IDS_FILE="$(mktemp)"
AGENT_ROOTS_FILE="$(mktemp)"
WORKSPACES_FILE="$(mktemp)"
TEAM_ROOTS_FILE="$(mktemp)"
TRANSIENT_FILE="$(mktemp)"

cleanup_tmp() {
  rm -f "$PLAN_FILE" "$AGENT_IDS_FILE" "$AGENT_ROOTS_FILE" "$WORKSPACES_FILE" "$TEAM_ROOTS_FILE" "$TRANSIENT_FILE"
}
trap cleanup_tmp EXIT HUP INT TERM

emit_purge_plan > "$PLAN_FILE"

if ! grep -q '^AGENT	' "$PLAN_FILE"; then
  printf 'No configured agents found in %s\n' "$CONFIG_PATH" >&2
  exit 1
fi

while IFS="$(printf '\t')" read -r kind c1 c2 c3 c4; do
  case "$kind" in
    AGENT)
      printf '%s\n' "$c1" >> "$AGENT_IDS_FILE"
      printf '%s\n' "$c4" >> "$AGENT_ROOTS_FILE"
      printf '%s\n' "$c2" >> "$WORKSPACES_FILE"
      ;;
    TEAM)
      printf '%s\n' "$c1" >> "$TEAM_ROOTS_FILE"
      ;;
    TRANSIENT)
      printf '%s\n' "$c1" >> "$TRANSIENT_FILE"
      ;;
  esac
done < "$PLAN_FILE"

sort -u "$AGENT_IDS_FILE" -o "$AGENT_IDS_FILE"
sort -u "$AGENT_ROOTS_FILE" -o "$AGENT_ROOTS_FILE"
sort -u "$WORKSPACES_FILE" -o "$WORKSPACES_FILE"
sort -u "$TEAM_ROOTS_FILE" -o "$TEAM_ROOTS_FILE"
sort -u "$TRANSIENT_FILE" -o "$TRANSIENT_FILE"

log "OpenClaw home: $OPENCLAW_HOME"
log "Config path: $CONFIG_PATH"

stop_openclaw_services
log 'Recalling persisted Feishu group messages before local purge'
recall_feishu_group_messages

while IFS= read -r agent_root; do
  [ -n "$agent_root" ] || continue
  purge_root "agent runtime" "$agent_root"
  reset_agent_state_files "$agent_root"
done < "$AGENT_ROOTS_FILE"

while IFS= read -r workspace_root; do
  [ -n "$workspace_root" ] || continue
  purge_root "workspace runtime" "$workspace_root"
  reset_workspace_state_files "$workspace_root"
  prune_workspace_outputs "$workspace_root" "$AGENT_IDS_FILE" "$WORKSPACES_FILE"
done < "$WORKSPACES_FILE"

while IFS= read -r team_root; do
  [ -n "$team_root" ] || continue
  purge_root "team shared runtime" "$team_root"
  reset_workspace_state_files "$team_root"
done < "$TEAM_ROOTS_FILE"

log 'Purging shared OpenClaw runtime state'
remove_dir_contents "$OPENCLAW_HOME/tasks"
remove_dir_contents "$OPENCLAW_HOME/flows"
remove_dir_contents "$OPENCLAW_HOME/subagents"
remove_dir_contents "$OPENCLAW_HOME/delivery-queue"
remove_dir_contents "$OPENCLAW_HOME/memory"
remove_dir_contents "$OPENCLAW_HOME/memories"
remove_dir_contents "$OPENCLAW_HOME/logs"
remove_path "$OPENCLAW_HOME/exec-approvals.json"

while IFS= read -r transient_root; do
  [ -n "$transient_root" ] || continue
  log "Purging transient runtime: $transient_root"
  remove_path "$transient_root"
done < "$TRANSIENT_FILE"

stabilize_runtime_after_restart
start_openclaw_gateway
check_openclaw_gateway_status
verify_no_resumed_work

log 'Done.'
