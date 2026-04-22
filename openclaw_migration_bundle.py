#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable


FORMAT_VERSION = 1
MANIFEST_NAME = "openclaw-migration.json"
PAYLOAD_DIRNAME = "payload"
EXPORT_MODES = ("skeleton", "full")

IGNORE_NAMES = {
    ".DS_Store",
    ".git",
    "__pycache__",
}
IGNORE_SUFFIXES = {
    ".lock",
    ".pyc",
    ".pyo",
    ".jsonl",
}

SKELETON_IGNORE_DIR_NAMES = {
    "sessions",
    "session-archives",
    "events",
    "artifacts",
    "executions",
    "projects",
    "archives",
    "memory",
    "memories",
    "logs",
    "reports",
    "runs",
    "snapshots",
    "tasks",
    "tasks_archive",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".openclaw",
    "generated",
    "specs",
}

SKELETON_IGNORE_FILE_NAMES = {
    "status.json",
    "handoff.json",
    "task.json",
    "workflow.json",
    "watchdog.json",
    "orchestrator.current-task.json",
    "orchestrator.state.json",
    "orchestrator.state.json.prev",
    "orchestrator.run.lock",
    "orchestrator.state.lock",
    "sessions.json",
    "auth-profiles.json",
    "auth-state.json",
    "team-status.json",
    "workspace-state.json",
    "group_sequence.json",
    "pyvenv.cfg",
    ".last-run.json",
}

MIGRATION_TOOL_NAMES = (
    "MIGRATION.md",
    "openclaw_migration_bundle.py",
    "export-openclaw-migration.sh",
    "import-openclaw-migration.sh",
    "export-openclaw-migration.bat",
    "import-openclaw-migration.bat",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export or import an OpenClaw agent/team migration bundle without "
            "overwriting the target machine's existing openclaw.json."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export", help="Export one agent or a full team bundle")
    export_parser.add_argument("--agent-id", required=True, help="Agent id to export")
    export_parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to create for the exported bundle; it will be created when missing",
    )
    export_parser.add_argument(
        "--source-home",
        default=os.environ.get("OPENCLAW_HOME", "~/.openclaw"),
        help="Source OpenClaw home directory (default: ~/.openclaw or OPENCLAW_HOME)",
    )
    export_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the export plan without writing the bundle directory",
    )
    export_parser.add_argument(
        "--export-mode",
        choices=EXPORT_MODES,
        default="skeleton",
        help=(
            "Export mode: 'skeleton' exports only team/agent skeleton files without "
            "sessions, memories, project history, or runtime state; 'full' exports "
            "the full current payload snapshot"
        ),
    )

    import_parser = sub.add_parser("import", help="Import a previously exported migration bundle")
    import_parser.add_argument(
        "--input-dir",
        required=True,
        help=f"Bundle directory that contains {MANIFEST_NAME}",
    )
    import_parser.add_argument(
        "--target-home",
        default=os.environ.get("OPENCLAW_HOME", "~/.openclaw"),
        help="Target OpenClaw home directory (default: ~/.openclaw or OPENCLAW_HOME)",
    )
    import_parser.add_argument(
        "--backup-suffix",
        default=".bak.migrate",
        help="Suffix for the target openclaw.json backup file",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the import plan without modifying the target",
    )
    return parser.parse_args()


def resolve_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def ensure_openclaw_home(home: Path) -> Path:
    config_path = home / "openclaw.json"
    if not config_path.is_file():
        raise SystemExit(f"missing openclaw.json under {home}")
    return config_path


def home_aliases(raw: str, resolved_home: Path) -> list[str]:
    aliases = {
        Path(os.path.expanduser(raw)).absolute().as_posix(),
        resolved_home.as_posix(),
    }
    return sorted(aliases, key=len, reverse=True)


def ensure_under_home(home: Path, path: Path, label: str) -> None:
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise SystemExit(f"{label} points outside source home: {path}") from exc


def normalize_agent_id(value: str) -> str:
    return value.strip().lower()


def index_agents(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {}
    for agent in config.get("agents", {}).get("list", []):
        agent_id = agent.get("id")
        if isinstance(agent_id, str) and agent_id:
            agents[agent_id] = agent
    return agents


def resolve_agent_ids(agent_id: str, agents_by_id: dict[str, dict[str, Any]]) -> list[str]:
    if agent_id not in agents_by_id:
        raise SystemExit(f"agent id not found in source openclaw.json: {agent_id}")

    selected: list[str] = []
    visited: set[str] = set()

    def visit(current_id: str) -> None:
        if current_id in visited:
            return
        visited.add(current_id)
        selected.append(current_id)
        current = agents_by_id[current_id]
        children = current.get("subagents", {}).get("allowAgents", [])
        if not isinstance(children, list):
            return
        for child_id in children:
            if isinstance(child_id, str) and child_id in agents_by_id:
                visit(child_id)

    visit(agent_id)
    return selected


def infer_team_root(workspace: Path, all_agent_ids: set[str]) -> Path | None:
    if (workspace / "_shared").exists():
        child_agent_dirs = 0
        for child in workspace.iterdir():
            if child.is_dir() and child.name in all_agent_ids:
                child_agent_dirs += 1
                if child_agent_dirs >= 1:
                    return workspace

    parent = workspace.parent
    if workspace.name not in all_agent_ids:
        return None
    if (parent / "_shared").exists():
        return parent

    sibling_agent_dirs = 0
    for child in parent.iterdir():
        if child.is_dir() and child.name in all_agent_ids:
            sibling_agent_dirs += 1
            if sibling_agent_dirs >= 2:
                return parent
    return None


def build_copy_plan(
    source_home: Path,
    selected_agent_ids: list[str],
    agents_by_id: dict[str, dict[str, Any]],
) -> list[tuple[Path, Path]]:
    all_agent_ids = set(agents_by_id)
    selected_workspaces: dict[str, Path] = {}
    selected_team_roots: dict[Path, set[str]] = {}
    standalone_workspaces: list[Path] = []

    for current_id in selected_agent_ids:
        workspace_value = agents_by_id[current_id].get("workspace")
        if not isinstance(workspace_value, str) or not workspace_value:
            continue
        workspace_path = Path(os.path.expanduser(workspace_value)).resolve()
        ensure_under_home(source_home, workspace_path, f"workspace for {current_id}")
        selected_workspaces[current_id] = workspace_path
        team_root = infer_team_root(workspace_path, all_agent_ids)
        if team_root is None:
            standalone_workspaces.append(workspace_path)
            continue
        selected_team_roots.setdefault(team_root, set()).add(current_id)

    plan: list[tuple[Path, Path]] = []
    seen: set[Path] = set()

    for team_root, selected_ids in sorted(selected_team_roots.items(), key=lambda item: str(item[0])):
        ensure_under_home(source_home, team_root, "team root")
        for child in sorted(team_root.iterdir(), key=lambda item: item.name):
            if should_ignore(child):
                continue
            include = child.name not in all_agent_ids or child.name in selected_ids
            if not include:
                continue
            rel = child.relative_to(source_home)
            if rel in seen:
                continue
            seen.add(rel)
            plan.append((child, rel))

    for workspace_path in sorted(standalone_workspaces, key=str):
        rel = workspace_path.relative_to(source_home)
        if rel in seen:
            continue
        seen.add(rel)
        plan.append((workspace_path, rel))

    for current_id in selected_agent_ids:
        agent_dir = source_home / "agents" / current_id
        if not agent_dir.exists():
            raise SystemExit(f"missing agent directory: {agent_dir}")
        rel = agent_dir.relative_to(source_home)
        if rel in seen:
            continue
        seen.add(rel)
        plan.append((agent_dir, rel))

    return plan


def replace_path_text(value: str, source_home_aliases: Iterable[str], target_home_text: str) -> str:
    rewritten = value
    for source_text in source_home_aliases:
        rewritten = rewritten.replace(source_text, target_home_text)
    return rewritten


def recursively_rewrite_paths(
    payload: Any,
    source_home_aliases: Iterable[str],
    target_home_text: str,
) -> Any:
    if isinstance(payload, str):
        return replace_path_text(payload, source_home_aliases, target_home_text)
    if isinstance(payload, list):
        return [recursively_rewrite_paths(item, source_home_aliases, target_home_text) for item in payload]
    if isinstance(payload, dict):
        rewritten = {
            key: recursively_rewrite_paths(value, source_home_aliases, target_home_text)
            for key, value in payload.items()
            if key != "requireAgentId"
        }
        subagents = rewritten.get("subagents")
        if isinstance(subagents, dict):
            subagents.pop("requireAgentId", None)
        return rewritten
    return payload


def should_ignore(path: Path, export_mode: str = "full") -> bool:
    if path.name in IGNORE_NAMES:
        return True
    if ".bak-" in path.name:
        return True
    if any(path.name.endswith(suffix) for suffix in IGNORE_SUFFIXES):
        return True
    if export_mode == "skeleton":
        if path.name.startswith(".venv"):
            return True
        if path.name in SKELETON_IGNORE_DIR_NAMES:
            return True
        if path.name in SKELETON_IGNORE_FILE_NAMES:
            return True
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
            return True
        if path.suffix == ".json" and "snapshot" in path.name:
            return True
    return False


def copy_entry(source: Path, destination: Path, *, export_mode: str) -> None:
    if should_ignore(source, export_mode):
        return
    if source.is_dir():
        destination.mkdir(parents=True, exist_ok=False)
        for child in sorted(source.iterdir(), key=lambda item: item.name):
            if should_ignore(child, export_mode):
                continue
            copy_entry(child, destination / child.name, export_mode=export_mode)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def rewrite_text_paths(root: Path, source_home_aliases: Iterable[str], target_home_text: str) -> None:
    paths = [root]
    if root.is_dir():
        paths = [path for path in root.rglob("*") if path.is_file()]
    for path in paths:
        if should_ignore(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rewritten = replace_path_text(text, source_home_aliases, target_home_text)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")


def copy_migration_tools(output_dir: Path) -> None:
    source_dir = Path(__file__).resolve().parent
    tools_dir = output_dir / "migration-tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    for name in MIGRATION_TOOL_NAMES:
        source = source_dir / name
        if source.is_file():
            shutil.copy2(source, tools_dir / name)


def selected_bindings(config: dict[str, Any], selected_agent_ids: set[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for binding in config.get("bindings", []):
        if binding.get("agentId") in selected_agent_ids:
            results.append(copy.deepcopy(binding))
    return results


def collect_channel_payload(
    config: dict[str, Any],
    bindings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    channels = config.get("channels", {})

    for binding in bindings:
        match = binding.get("match", {})
        channel_type = match.get("channel")
        account_id = match.get("accountId")
        if not isinstance(channel_type, str) or not channel_type:
            continue

        source_section = channels.get(channel_type, {})
        if not isinstance(source_section, dict):
            continue

        entry = payload.setdefault(
            channel_type,
            {
                "shared": copy.deepcopy({key: value for key, value in source_section.items() if key != "accounts"}),
                "accounts": {},
            },
        )
        accounts = source_section.get("accounts", {})
        if isinstance(account_id, str) and account_id and isinstance(accounts, dict) and account_id in accounts:
            entry["accounts"][account_id] = copy.deepcopy(accounts[account_id])

    return payload


def collect_plugin_payload(config: dict[str, Any], channel_types: Iterable[str]) -> dict[str, Any]:
    plugins = config.get("plugins", {})
    allow = plugins.get("allow", [])
    entries = plugins.get("entries", {})
    selected = set(channel_types)

    return {
        "enabled": plugins.get("enabled"),
        "allow": [
            item
            for item in allow
            if isinstance(item, str) and item in selected
        ],
        "entries": {
            plugin_id: copy.deepcopy(entry)
            for plugin_id, entry in entries.items()
            if plugin_id in selected and isinstance(entry, dict)
        },
    }


def collect_model_payload(config: dict[str, Any], selected_agent_ids: Iterable[str]) -> dict[str, Any]:
    model_defs = config.get("agents", {}).get("defaults", {}).get("models", {})
    referenced: set[str] = set()

    for agent in config.get("agents", {}).get("list", []):
        if agent.get("id") not in selected_agent_ids:
            continue
        model = agent.get("model", {})
        if isinstance(model, dict):
            primary = model.get("primary")
            if isinstance(primary, str):
                referenced.add(primary)
            fallbacks = model.get("fallbacks", [])
            if isinstance(fallbacks, list):
                referenced.update(item for item in fallbacks if isinstance(item, str))
        elif isinstance(model, str):
            referenced.add(model)

        heartbeat = agent.get("heartbeat", {})
        if isinstance(heartbeat, dict):
            heartbeat_model = heartbeat.get("model")
            if isinstance(heartbeat_model, str):
                referenced.add(heartbeat_model)

    return {
        model_id: copy.deepcopy(model_defs[model_id])
        for model_id in sorted(referenced)
        if model_id in model_defs
    }


def ensure_empty_or_missing_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise SystemExit(f"output path exists and is not a directory: {path}")
        if any(path.iterdir()):
            raise SystemExit(f"output directory is not empty: {path}")
    else:
        path.mkdir(parents=True, exist_ok=True)


def export_bundle(
    *,
    source_home: Path,
    source_home_aliases: list[str],
    agent_id: str,
    output_dir: Path,
    dry_run: bool,
    export_mode: str,
) -> dict[str, Any]:
    source_config = load_json(ensure_openclaw_home(source_home))
    agents_by_id = index_agents(source_config)
    selected_agent_ids = resolve_agent_ids(agent_id, agents_by_id)
    bindings = selected_bindings(source_config, set(selected_agent_ids))
    channel_payload = collect_channel_payload(source_config, bindings)
    copy_plan = [
        (source_path, rel)
        for source_path, rel in build_copy_plan(source_home, selected_agent_ids, agents_by_id)
        if not should_ignore(source_path, export_mode)
    ]
    manifest = {
        "kind": "openclaw-agent-migration",
        "formatVersion": FORMAT_VERSION,
        "mode": "team" if len(selected_agent_ids) > 1 else "single-agent",
        "exportMode": export_mode,
        "requestedAgentId": agent_id,
        "selectedAgentIds": selected_agent_ids,
        "source": {
            "openClawHome": source_home.as_posix(),
            "openClawHomeAliases": source_home_aliases,
        },
        "payloadDir": PAYLOAD_DIRNAME,
        "copyEntries": [rel.as_posix() for _, rel in copy_plan],
        "configPatch": {
            "agents": [
                copy.deepcopy(agent)
                for agent in source_config.get("agents", {}).get("list", [])
                if agent.get("id") in set(selected_agent_ids)
            ],
            "bindings": bindings,
            "channels": channel_payload,
            "plugins": collect_plugin_payload(source_config, channel_payload.keys()),
            "models": collect_model_payload(source_config, selected_agent_ids),
        },
    }

    if dry_run:
        return {
            "mode": manifest["mode"],
            "exportMode": export_mode,
            "requestedAgentId": agent_id,
            "selectedAgentIds": selected_agent_ids,
            "outputDir": output_dir.as_posix(),
            "manifestPath": (output_dir / MANIFEST_NAME).as_posix(),
            "payloadDir": (output_dir / PAYLOAD_DIRNAME).as_posix(),
            "copyEntries": manifest["copyEntries"],
        }

    ensure_empty_or_missing_dir(output_dir)
    payload_root = output_dir / PAYLOAD_DIRNAME
    payload_root.mkdir(parents=True, exist_ok=True)

    for source_path, rel in copy_plan:
        copy_entry(source_path, payload_root / rel, export_mode=export_mode)

    copy_migration_tools(output_dir)
    dump_json(output_dir / MANIFEST_NAME, manifest)
    return {
        "mode": manifest["mode"],
        "exportMode": export_mode,
        "requestedAgentId": agent_id,
        "selectedAgentIds": selected_agent_ids,
        "manifestPath": (output_dir / MANIFEST_NAME).as_posix(),
        "payloadDir": payload_root.as_posix(),
    }


def detect_import_conflicts(
    target_config: dict[str, Any],
    manifest: dict[str, Any],
    target_home: Path,
) -> list[str]:
    conflicts: list[str] = []
    selected_agent_ids = set(manifest.get("selectedAgentIds", []))

    target_agent_ids = {
        agent.get("id")
        for agent in target_config.get("agents", {}).get("list", [])
        if isinstance(agent.get("id"), str)
    }
    for agent_id in sorted(selected_agent_ids & target_agent_ids):
        conflicts.append(f"target openclaw.json already has agent {agent_id}")

    channels = target_config.get("channels", {})
    for channel_type, channel_payload in manifest.get("configPatch", {}).get("channels", {}).items():
        target_accounts = {}
        if isinstance(channels.get(channel_type), dict):
            target_accounts = channels[channel_type].get("accounts", {})
        if not isinstance(target_accounts, dict):
            target_accounts = {}
        source_accounts = channel_payload.get("accounts", {})
        if not isinstance(source_accounts, dict):
            continue
        for account_id in sorted(source_accounts):
            if account_id in target_accounts:
                conflicts.append(f"target openclaw.json already has {channel_type} account {account_id}")

    target_bindings = target_config.get("bindings", [])
    target_binding_keys = set()
    for binding in target_bindings:
        if not isinstance(binding, dict):
            continue
        agent = binding.get("agentId")
        match = binding.get("match", {})
        channel = match.get("channel") if isinstance(match, dict) else None
        account = match.get("accountId") if isinstance(match, dict) else None
        target_binding_keys.add((agent, channel, account))

    for binding in manifest.get("configPatch", {}).get("bindings", []):
        agent = binding.get("agentId")
        match = binding.get("match", {})
        channel = match.get("channel") if isinstance(match, dict) else None
        account = match.get("accountId") if isinstance(match, dict) else None
        if agent in selected_agent_ids and any(existing[0] == agent for existing in target_binding_keys):
            conflicts.append(f"target openclaw.json already has binding slot for {agent}")
        if (agent, channel, account) in target_binding_keys:
            conflicts.append(f"target openclaw.json already has binding {agent}:{channel}:{account}")

    for rel_text in manifest.get("copyEntries", []):
        if not isinstance(rel_text, str) or not rel_text:
            continue
        destination = target_home / rel_text
        if destination.exists():
            conflicts.append(f"target path already exists: {destination}")

    deduped: list[str] = []
    seen: set[str] = set()
    for item in conflicts:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def merge_missing(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        if isinstance(target[key], dict) and isinstance(value, dict):
            merge_missing(target[key], value)


def merge_plugins(target_config: dict[str, Any], plugin_payload: dict[str, Any]) -> None:
    if not plugin_payload:
        return
    plugins = target_config.setdefault("plugins", {})
    if plugin_payload.get("enabled") and "enabled" not in plugins:
        plugins["enabled"] = True

    allow = plugins.setdefault("allow", [])
    if not isinstance(allow, list):
        allow = []
        plugins["allow"] = allow
    for plugin_id in plugin_payload.get("allow", []):
        if plugin_id not in allow:
            allow.append(plugin_id)

    entries = plugins.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        plugins["entries"] = entries
    for plugin_id, source_entry in plugin_payload.get("entries", {}).items():
        if plugin_id not in entries or not isinstance(entries[plugin_id], dict):
            entries[plugin_id] = copy.deepcopy(source_entry)
            continue
        merge_missing(entries[plugin_id], source_entry)
        if source_entry.get("enabled"):
            entries[plugin_id]["enabled"] = True


def merge_channels(target_config: dict[str, Any], channel_payload: dict[str, Any]) -> None:
    channels = target_config.setdefault("channels", {})
    for channel_type, source_section in channel_payload.items():
        target_section = channels.setdefault(channel_type, {})
        if not isinstance(target_section, dict):
            raise SystemExit(f"target channel section is not an object: {channel_type}")
        shared = source_section.get("shared", {})
        if isinstance(shared, dict):
            merge_missing(target_section, shared)
            if shared.get("enabled"):
                target_section["enabled"] = True

        target_accounts = target_section.setdefault("accounts", {})
        if not isinstance(target_accounts, dict):
            raise SystemExit(f"target accounts section is not an object: {channel_type}")
        source_accounts = source_section.get("accounts", {})
        if not isinstance(source_accounts, dict):
            continue
        for account_id, account in source_accounts.items():
            target_accounts[account_id] = copy.deepcopy(account)


def merge_models(target_config: dict[str, Any], model_payload: dict[str, Any]) -> None:
    if not model_payload:
        return
    target_models = (
        target_config.setdefault("agents", {})
        .setdefault("defaults", {})
        .setdefault("models", {})
    )
    if not isinstance(target_models, dict):
        raise SystemExit("target agents.defaults.models is not an object")
    for model_id, model_def in model_payload.items():
        if model_id not in target_models:
            target_models[model_id] = copy.deepcopy(model_def)


def import_bundle(
    *,
    input_dir: Path,
    target_home: Path,
    backup_suffix: str,
    dry_run: bool,
) -> dict[str, Any]:
    manifest_path = input_dir / MANIFEST_NAME
    payload_root = input_dir / PAYLOAD_DIRNAME
    if not manifest_path.is_file():
        raise SystemExit(f"missing migration manifest: {manifest_path}")
    if not payload_root.is_dir():
        raise SystemExit(f"missing payload directory: {payload_root}")

    manifest = load_json(manifest_path)
    if manifest.get("kind") != "openclaw-agent-migration":
        raise SystemExit(f"unsupported migration manifest kind: {manifest.get('kind')}")
    if manifest.get("formatVersion") != FORMAT_VERSION:
        raise SystemExit(
            f"unsupported migration manifest version: {manifest.get('formatVersion')}"
        )

    target_config_path = ensure_openclaw_home(target_home)
    target_config = load_json(target_config_path)

    source_aliases = manifest.get("source", {}).get("openClawHomeAliases", [])
    if not isinstance(source_aliases, list) or not all(isinstance(item, str) for item in source_aliases):
        raise SystemExit("migration manifest is missing source openClawHomeAliases")
    target_home_text = target_home.as_posix()

    conflicts = detect_import_conflicts(target_config, manifest, target_home)
    if conflicts:
        raise SystemExit("refusing to overwrite target state:\n- " + "\n- ".join(conflicts))

    planned_paths = [
        (payload_root / rel_text).as_posix()
        for rel_text in manifest.get("copyEntries", [])
        if isinstance(rel_text, str)
    ]
    config_backup = f"{target_config_path}{backup_suffix}"

    if dry_run:
        return {
            "mode": manifest.get("mode"),
            "requestedAgentId": manifest.get("requestedAgentId"),
            "selectedAgentIds": manifest.get("selectedAgentIds"),
            "manifestPath": manifest_path.as_posix(),
            "copySources": planned_paths,
            "targetHome": target_home.as_posix(),
            "configBackup": config_backup,
        }

    backup_path = Path(config_backup)
    if backup_path.exists():
        raise SystemExit(f"target backup file already exists: {backup_path}")
    shutil.copy2(target_config_path, backup_path)

    for rel_text in manifest.get("copyEntries", []):
        if not isinstance(rel_text, str) or not rel_text:
            continue
        source_path = payload_root / rel_text
        if not source_path.exists():
            raise SystemExit(f"missing payload entry: {source_path}")
        destination = target_home / rel_text
        copy_entry(
            source_path,
            destination,
            export_mode=str(manifest.get("exportMode") or "full"),
        )
        rewrite_text_paths(destination, source_aliases, target_home_text)

    selected_agents = [
        recursively_rewrite_paths(agent, source_aliases, target_home_text)
        for agent in manifest.get("configPatch", {}).get("agents", [])
    ]
    target_config.setdefault("agents", {}).setdefault("list", []).extend(selected_agents)

    merge_models(
        target_config,
        recursively_rewrite_paths(
            manifest.get("configPatch", {}).get("models", {}),
            source_aliases,
            target_home_text,
        ),
    )
    merge_channels(
        target_config,
        recursively_rewrite_paths(
            manifest.get("configPatch", {}).get("channels", {}),
            source_aliases,
            target_home_text,
        ),
    )
    merge_plugins(
        target_config,
        recursively_rewrite_paths(
            manifest.get("configPatch", {}).get("plugins", {}),
            source_aliases,
            target_home_text,
        ),
    )

    target_bindings = target_config.setdefault("bindings", [])
    target_bindings.extend(
        recursively_rewrite_paths(
            manifest.get("configPatch", {}).get("bindings", []),
            source_aliases,
            target_home_text,
        )
    )
    dump_json(target_config_path, target_config)

    return {
        "mode": manifest.get("mode"),
        "requestedAgentId": manifest.get("requestedAgentId"),
        "selectedAgentIds": manifest.get("selectedAgentIds"),
        "manifestPath": manifest_path.as_posix(),
        "targetHome": target_home.as_posix(),
        "configBackup": backup_path.as_posix(),
    }


def main() -> None:
    args = parse_args()

    if args.command == "export":
        source_home = resolve_path(args.source_home)
        result = export_bundle(
            source_home=source_home,
            source_home_aliases=home_aliases(args.source_home, source_home),
            agent_id=args.agent_id,
            output_dir=resolve_path(args.output_dir),
            dry_run=args.dry_run,
            export_mode=args.export_mode,
        )
    elif args.command == "import":
        result = import_bundle(
            input_dir=resolve_path(args.input_dir),
            target_home=resolve_path(args.target_home),
            backup_suffix=args.backup_suffix,
            dry_run=args.dry_run,
        )
    else:
        raise SystemExit(f"unsupported command: {args.command}")

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
