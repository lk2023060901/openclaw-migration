#!/usr/bin/env sh
set -eu

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
DEV_ROOT="$OPENCLAW_HOME/workspace/development"
AGENTS_ROOT="$OPENCLAW_HOME/agents"

DRY_RUN=0
ASSUME_YES=0
PURGE_SHARED_MEMORY=0
PURGE_SHARED_RUNTIME=0

usage() {
  cat <<'USAGE'
Usage:
  purge-development-state.sh --yes [--dry-run] [--purge-shared-memory] [--purge-shared-runtime]

Execution flow:
  1. Stop OpenClaw daemon
  2. Stop OpenClaw gateway
  3. Purge development state
  4. Start OpenClaw daemon
  5. Start OpenClaw gateway
  6. Check daemon and gateway status

What it clears by default:
  - development team agent session history (*.jsonl)
  - development team session indexes (sessions.json)
  - development workspace events/*.json
  - development agent task/status/handoff state JSON
  - project-manager workflow/team-status JSON
  - development projects/ and archives/
  - development shared port registry

Optional shared cleanup:
  --purge-shared-memory   Also clear ~/.openclaw/memory
                          Warning: this is shared across agents if present.
  --purge-shared-runtime  Also clear ~/.openclaw/tasks, flows, subagents,
                          delivery-queue, feishu/dedup.

Environment:
  OPENCLAW_HOME           Override OpenClaw home (default: ~/.openclaw)
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

reset_agent_workspace_state() {
  workspace_dir="$1"

  if [ -d "$workspace_dir/events" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      find "$workspace_dir/events" -type f -name '*.json' -print | sed 's#^#[dry-run] rm #' || true
    else
      find "$workspace_dir/events" -type f -name '*.json' -delete || true
    fi
  fi

  if [ -f "$workspace_dir/task.json" ]; then
    write_json "$workspace_dir/task.json" '{"task_id":null,"title":null,"status":"idle","current_stage":null}'
  fi
  if [ -f "$workspace_dir/status.json" ]; then
    write_json "$workspace_dir/status.json" '{"status":"idle","current_stage":null,"last_group_reply":null}'
  fi
  if [ -f "$workspace_dir/handoff.json" ]; then
    write_json "$workspace_dir/handoff.json" '{"project_name":null,"from":null,"to":null,"artifacts":[],"notes":null}'
  fi
}

reset_project_manager_state() {
  pm_dir="$DEV_ROOT/project-manager"
  if [ -f "$pm_dir/workflow.json" ]; then
    write_json "$pm_dir/workflow.json" '{"project_name":null,"current_phase":null,"active_task_id":null,"phases":[],"blocked":false,"block_reason":null}'
  fi
  if [ -f "$pm_dir/team-status.json" ]; then
    write_json "$pm_dir/team-status.json" '{"project_name":null,"phase":null,"agents":{}}'
  fi
}

list_dev_agents() {
  find "$DEV_ROOT" -mindepth 1 -maxdepth 1 -type d \
    ! -name '_shared' \
    ! -name 'projects' \
    ! -name 'archives' \
    -exec basename {} \; | sort
}

stop_openclaw_services() {
  log 'Stopping OpenClaw daemon'
  run_soft openclaw daemon stop
  log 'Stopping OpenClaw gateway'
  run_soft openclaw gateway stop
}

start_openclaw_services() {
  log 'Starting OpenClaw daemon'
  run_soft openclaw daemon start
  log 'Starting OpenClaw gateway'
  run_soft openclaw gateway start
}

check_openclaw_status() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log '[dry-run] openclaw daemon status'
    log '[dry-run] openclaw gateway status'
    return 0
  fi

  daemon_attempts=3
  daemon_try=1
  while [ "$daemon_try" -le "$daemon_attempts" ]; do
    if openclaw daemon status; then
      break
    fi
    if [ "$daemon_try" -eq "$daemon_attempts" ]; then
      return 1
    fi
    sleep 2
    daemon_try=$((daemon_try + 1))
  done

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

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|--force)
      ASSUME_YES=1
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    --purge-shared-memory)
      PURGE_SHARED_MEMORY=1
      ;;
    --purge-shared-runtime)
      PURGE_SHARED_RUNTIME=1
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
  printf 'Refusing to run without --yes. This script permanently deletes development state.\n' >&2
  exit 1
fi

if [ ! -d "$DEV_ROOT" ]; then
  printf 'Development workspace not found: %s\n' "$DEV_ROOT" >&2
  exit 1
fi

log "OpenClaw home: $OPENCLAW_HOME"
log "Development root: $DEV_ROOT"

stop_openclaw_services

for agent_id in $(list_dev_agents); do
  agent_dir="$AGENTS_ROOT/$agent_id"
  workspace_dir="$DEV_ROOT/$agent_id"

  log "Cleaning agent: $agent_id"

  if [ -d "$agent_dir/sessions" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      find "$agent_dir/sessions" -type f -name '*.jsonl' -print | sed 's#^#[dry-run] rm #' || true
    else
      find "$agent_dir/sessions" -type f -name '*.jsonl' -delete || true
    fi
  fi

  if [ -d "$agent_dir" ] || [ -f "$agent_dir/sessions.json" ]; then
    write_json "$agent_dir/sessions.json" '{}'
  fi

  reset_agent_workspace_state "$workspace_dir"
done

if [ -f "$DEV_ROOT/_shared/development-ports.json" ]; then
  write_json "$DEV_ROOT/_shared/development-ports.json" '{"shared":[],"projects":{}}'
fi

reset_project_manager_state

remove_dir_contents "$DEV_ROOT/projects"
remove_dir_contents "$DEV_ROOT/archives"
run mkdir -p "$DEV_ROOT/projects" "$DEV_ROOT/archives"

if [ "$PURGE_SHARED_MEMORY" -eq 1 ]; then
  log 'Purging shared memory store'
  remove_dir_contents "$OPENCLAW_HOME/memory"
fi

if [ "$PURGE_SHARED_RUNTIME" -eq 1 ]; then
  log 'Purging shared runtime state'
  remove_dir_contents "$OPENCLAW_HOME/tasks"
  remove_dir_contents "$OPENCLAW_HOME/flows"
  remove_dir_contents "$OPENCLAW_HOME/subagents"
  remove_dir_contents "$OPENCLAW_HOME/delivery-queue"
  remove_dir_contents "$OPENCLAW_HOME/feishu/dedup"
fi

start_openclaw_services
check_openclaw_status

log 'Done.'
