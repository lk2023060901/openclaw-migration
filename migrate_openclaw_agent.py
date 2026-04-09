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


IGNORE_NAMES = {
    ".DS_Store",
    "__pycache__",
}
IGNORE_SUFFIXES = {
    ".lock",
    ".pyc",
    ".pyo",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate one OpenClaw agent, or a full team when the agent controls "
            "subagents, into another OpenClaw home without overwriting the target config."
        )
    )
    parser.add_argument("--source-home", required=True, help="Source OpenClaw home directory")
    parser.add_argument("--target-home", required=True, help="Target OpenClaw home directory")
    parser.add_argument("--agent-id", required=True, help="Agent id to migrate")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migration plan without modifying the target",
    )
    parser.add_argument(
        "--backup-suffix",
        default=".bak.migrate",
        help="Suffix for the target openclaw.json backup file",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def resolve_home(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def home_text(raw: str) -> str:
    return Path(os.path.expanduser(raw)).absolute().as_posix()


def ensure_openclaw_home(home: Path) -> Path:
    config = home / "openclaw.json"
    if not config.is_file():
        raise SystemExit(f"missing openclaw.json under {home}")
    return config


def ensure_under_home(home: Path, path: Path, label: str) -> None:
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise SystemExit(f"{label} points outside source home: {path}") from exc


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

    visited: set[str] = set()
    order: list[str] = []

    def visit(current_id: str) -> None:
        if current_id in visited:
            return
        visited.add(current_id)
        order.append(current_id)
        current = agents_by_id[current_id]
        children = current.get("subagents", {}).get("allowAgents", [])
        if not isinstance(children, list) or not children:
            return
        for child_id in children:
            if isinstance(child_id, str) and child_id in agents_by_id:
                visit(child_id)

    visit(agent_id)
    return order


def infer_team_root(workspace: Path, all_agent_ids: set[str]) -> Path | None:
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
    selected_ids = set(selected_agent_ids)
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

    for team_root, team_selected_ids in sorted(selected_team_roots.items(), key=lambda item: str(item[0])):
        ensure_under_home(source_home, team_root, "team root")
        for child in sorted(team_root.iterdir(), key=lambda item: item.name):
            include = child.name not in all_agent_ids or child.name in team_selected_ids
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


def replace_path_text(value: str, source_home_texts: Iterable[str], target_home_text: str) -> str:
    rewritten = value
    for source_home_text in source_home_texts:
        rewritten = rewritten.replace(source_home_text, target_home_text)
    return rewritten


def recursively_rewrite_paths(
    payload: Any,
    source_home_texts: Iterable[str],
    target_home_text: str,
) -> Any:
    if isinstance(payload, str):
        return replace_path_text(payload, source_home_texts, target_home_text)
    if isinstance(payload, list):
        return [recursively_rewrite_paths(item, source_home_texts, target_home_text) for item in payload]
    if isinstance(payload, dict):
        rewritten = {
            key: recursively_rewrite_paths(value, source_home_texts, target_home_text)
            for key, value in payload.items()
            if not (key == "requireAgentId" and isinstance(payload, dict))
        }
        subagents = rewritten.get("subagents")
        if isinstance(subagents, dict):
            subagents.pop("requireAgentId", None)
        return rewritten
    return payload


def merge_models(
    source_config: dict[str, Any],
    target_config: dict[str, Any],
    selected_agent_ids: Iterable[str],
) -> None:
    source_models = source_config.get("agents", {}).get("defaults", {}).get("models", {})
    target_models = target_config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
    referenced: set[str] = set()

    for agent in source_config.get("agents", {}).get("list", []):
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
        heartbeat = agent.get("heartbeat", {})
        if isinstance(heartbeat, dict):
            heartbeat_model = heartbeat.get("model")
            if isinstance(heartbeat_model, str):
                referenced.add(heartbeat_model)

    for model_id in sorted(referenced):
        if model_id in target_models or model_id not in source_models:
            continue
        target_models[model_id] = copy.deepcopy(source_models[model_id])


def collect_bindings(config: dict[str, Any], selected_agent_ids: set[str]) -> list[dict[str, Any]]:
    bindings = []
    for binding in config.get("bindings", []):
        if binding.get("agentId") in selected_agent_ids:
            bindings.append(copy.deepcopy(binding))
    return bindings


def detect_conflicts(
    target_config: dict[str, Any],
    selected_agent_ids: list[str],
    copy_plan: list[tuple[Path, Path]],
    target_home: Path,
) -> list[str]:
    conflicts: list[str] = []
    selected = set(selected_agent_ids)

    target_agent_ids = {
        agent.get("id")
        for agent in target_config.get("agents", {}).get("list", [])
        if isinstance(agent.get("id"), str)
    }
    for agent_id in sorted(selected & target_agent_ids):
        conflicts.append(f"target openclaw.json already has agent {agent_id}")

    accounts = target_config.get("channels", {}).get("feishu", {}).get("accounts", {})
    for agent_id in sorted(selected):
        if agent_id in accounts:
            conflicts.append(f"target openclaw.json already has feishu account {agent_id}")

    binding_ids = {
        binding.get("agentId")
        for binding in target_config.get("bindings", [])
        if isinstance(binding.get("agentId"), str)
    }
    for agent_id in sorted(selected & binding_ids):
        conflicts.append(f"target openclaw.json already has binding for {agent_id}")

    for _, rel in copy_plan:
        dest = target_home / rel
        if dest.exists():
            conflicts.append(f"target path already exists: {dest}")

    return conflicts


def should_ignore(path: Path) -> bool:
    if path.name in IGNORE_NAMES:
        return True
    return any(path.name.endswith(suffix) for suffix in IGNORE_SUFFIXES)


def copy_entry(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=False,
            ignore=shutil.ignore_patterns(*IGNORE_NAMES, "*.lock", "*.pyc", "*.pyo"),
        )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def rewrite_text_paths(root: Path, source_home_texts: Iterable[str], target_home_text: str) -> None:
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
        rewritten = replace_path_text(text, source_home_texts, target_home_text)
        if rewritten == text:
            continue
        path.write_text(rewritten, encoding="utf-8")


def migrate(
    source_home: Path,
    target_home: Path,
    source_home_texts: list[str],
    target_home_text: str,
    agent_id: str,
    dry_run: bool,
    backup_suffix: str,
) -> dict[str, Any]:
    source_config = load_json(ensure_openclaw_home(source_home))
    target_config_path = ensure_openclaw_home(target_home)
    target_config = load_json(target_config_path)

    agents_by_id = index_agents(source_config)
    selected_agent_ids = resolve_agent_ids(agent_id, agents_by_id)
    source_agent_ids = set(selected_agent_ids)
    copy_plan = build_copy_plan(source_home, selected_agent_ids, agents_by_id)

    conflicts = detect_conflicts(target_config, selected_agent_ids, copy_plan, target_home)
    if conflicts:
        raise SystemExit("refusing to overwrite target state:\n- " + "\n- ".join(conflicts))

    controller_mode = len(selected_agent_ids) > 1

    planned_paths = [str(target_home / rel) for _, rel in copy_plan]

    if dry_run:
        return {
            "mode": "team" if controller_mode else "single-agent",
            "agentId": agent_id,
            "selectedAgentIds": selected_agent_ids,
            "copyTargets": planned_paths,
            "configBackup": str(target_config_path) + backup_suffix,
            "warning": "global subagent registries are not merged; a migrated controller may recover by heartbeat redispatch after cutover",
        }

    target_config_backup = Path(str(target_config_path) + backup_suffix)
    if target_config_backup.exists():
        raise SystemExit(f"target backup file already exists: {target_config_backup}")
    shutil.copy2(target_config_path, target_config_backup)

    for source_path, rel in copy_plan:
        destination = target_home / rel
        copy_entry(source_path, destination)
        rewrite_text_paths(destination, source_home_texts, target_home_text)

    selected_agents = []
    for agent in source_config.get("agents", {}).get("list", []):
        current_id = agent.get("id")
        if current_id not in source_agent_ids:
            continue
        rewritten = recursively_rewrite_paths(copy.deepcopy(agent), source_home_texts, target_home_text)
        selected_agents.append(rewritten)

    target_config.setdefault("agents", {}).setdefault("list", []).extend(selected_agents)
    merge_models(source_config, target_config, selected_agent_ids)

    source_accounts = source_config.get("channels", {}).get("feishu", {}).get("accounts", {})
    target_accounts = (
        target_config.setdefault("channels", {})
        .setdefault("feishu", {})
        .setdefault("accounts", {})
    )
    for current_id in selected_agent_ids:
        if current_id not in source_accounts:
            continue
        target_accounts[current_id] = copy.deepcopy(source_accounts[current_id])

    target_bindings = target_config.setdefault("bindings", [])
    target_bindings.extend(collect_bindings(source_config, source_agent_ids))

    dump_json(target_config_path, target_config)

    return {
        "mode": "team" if controller_mode else "single-agent",
        "agentId": agent_id,
        "selectedAgentIds": selected_agent_ids,
        "copyTargets": planned_paths,
        "configBackup": str(target_config_backup),
        "warning": "global subagent registries are not merged; a migrated controller may recover by heartbeat redispatch after cutover",
    }


def main() -> None:
    args = parse_args()
    source_home = resolve_home(args.source_home)
    target_home = resolve_home(args.target_home)
    source_home_texts = sorted({home_text(args.source_home), source_home.as_posix()}, key=len, reverse=True)
    target_home_text = home_text(args.target_home)

    if source_home == target_home:
        raise SystemExit("source-home and target-home must be different")

    result = migrate(
        source_home=source_home,
        target_home=target_home,
        source_home_texts=source_home_texts,
        target_home_text=target_home_text,
        agent_id=args.agent_id,
        dry_run=args.dry_run,
        backup_suffix=args.backup_suffix,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
