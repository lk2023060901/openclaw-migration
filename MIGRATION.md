# OpenClaw Agent Migration Scripts

This directory contains the migration tools for exporting and importing OpenClaw agent or team configuration bundles.

## Files

- `export-openclaw-migration.sh`
- `import-openclaw-migration.sh`
- `export-openclaw-migration.bat`
- `import-openclaw-migration.bat`
- `openclaw_migration_bundle.py`
- `migrate_openclaw_agent.py`
- `migrate_openclaw_agent.bat`

## Recommended Flow

Use the bundle-based flow by default:

1. Export from the source machine.
2. Copy the exported bundle directory to the target machine.
3. Import the bundle into the target machine's `~/.openclaw`.

Recommended entrypoints:

- macOS / Linux export: `export-openclaw-migration.sh`
- macOS / Linux import: `import-openclaw-migration.sh`
- Windows export: `export-openclaw-migration.bat`
- Windows import: `import-openclaw-migration.bat`

By default, export uses `skeleton` mode.
That means the bundle contains the team or agent skeleton only, not historical runtime data from the source machine.

## Script Overview

### `export-openclaw-migration.sh`

Wrapper for:

```bash
python3 openclaw_migration_bundle.py export ...
```

This exports one agent, or a whole team when the selected agent controls subagents, into a portable bundle directory.
Accepted parameters: same as `openclaw_migration_bundle.py export`

Default behavior:

- export mode defaults to `skeleton`
- the output directory will be created automatically when missing
- the output directory must be empty when it already exists

### `import-openclaw-migration.sh`

Wrapper for:

```bash
python3 openclaw_migration_bundle.py import ...
```

This imports a previously exported bundle into an existing OpenClaw home without overwriting the target machine's existing `openclaw.json`.
Accepted parameters: same as `openclaw_migration_bundle.py import`

### `export-openclaw-migration.bat`

Windows wrapper for:

```bat
py -3 openclaw_migration_bundle.py export ...
```

If `py` is not available, it falls back to `python`.
Accepted parameters: same as `openclaw_migration_bundle.py export`

### `import-openclaw-migration.bat`

Windows wrapper for:

```bat
py -3 openclaw_migration_bundle.py import ...
```

If `py` is not available, it falls back to `python`.
Accepted parameters: same as `openclaw_migration_bundle.py import`

### `migrate_openclaw_agent.py`

Legacy direct migration tool.

It copies from one OpenClaw home directly into another OpenClaw home in one step. This is useful when both source and target OpenClaw directories are directly reachable from the same machine.

Prefer the export/import flow when moving data between different machines.

Important:

- `migrate_openclaw_agent.py` is a legacy direct-copy tool
- it does not implement the new `skeleton` export semantics
- prefer the bundle-based export/import flow when you want to ship only team skeleton files

### `migrate_openclaw_agent.bat`

Windows wrapper for `migrate_openclaw_agent.py`.
Accepted parameters: same as `migrate_openclaw_agent.py`

## Parameters

The `.sh` and `.bat` wrappers do not add their own custom options.
They only forward arguments to the underlying Python entrypoint.

## `openclaw_migration_bundle.py export`

Usage:

```bash
python3 openclaw_migration_bundle.py export --agent-id AGENT_ID --output-dir OUTPUT_DIR [options]
```

Parameters:

- `--agent-id`
  Meaning: The agent id to export.
  Behavior:
  - If the agent controls subagents, the whole team is exported.
  - If the agent is a normal member agent, only that agent is exported.
  Required: Yes
  Optional values: Any agent id that already exists in the source `openclaw.json`.

- `--output-dir`
  Meaning: The bundle output directory to create.
  Behavior:
  - The directory will be created automatically if it does not exist.
  - The directory must be empty if it already exists.
  Required: Yes
  Optional values: Any writable path.

- `--source-home`
  Meaning: Source OpenClaw home directory.
  Default: `OPENCLAW_HOME` environment variable if set, otherwise `~/.openclaw`
  Required: No
  Optional values: Any directory that contains `openclaw.json`.

- `--dry-run`
  Meaning: Print the export plan without creating the bundle.
  Default: Disabled
  Required: No
  Optional values:
  - present
  - omitted

- `--export-mode`
  Meaning: Controls whether export contains only the reusable team skeleton or the full current runtime snapshot.
  Default: `skeleton`
  Required: No
  Optional values:
  - `skeleton`
    Behavior:
    - exports only reusable team or agent skeleton files
    - excludes agent sessions
    - excludes auth profiles
    - excludes runtime state files such as `status.json`, `handoff.json`, `task.json`, `workflow.json`, `watchdog.json`
    - excludes historical work directories such as `events/`, `artifacts/`, `projects/`, `archives/`, `memory/`, `memories/`, `logs/`, `snapshots/`
    - suitable for shipping a team template such as `development` without the source machine's historical projects
  - `full`
    Behavior:
    - exports the full currently selected payload snapshot, including runtime state that is not filtered by the standard ignore rules
    - use only when you intentionally want a closer runtime copy

## `openclaw_migration_bundle.py import`

Usage:

```bash
python3 openclaw_migration_bundle.py import --input-dir INPUT_DIR [options]
```

Parameters:

- `--input-dir`
  Meaning: The exported bundle directory to import.
  Required: Yes
  Optional values: Any directory that contains:
  - `openclaw-migration.json`
  - `payload/`

- `--target-home`
  Meaning: Target OpenClaw home directory.
  Default: `OPENCLAW_HOME` environment variable if set, otherwise `~/.openclaw`
  Required: No
  Optional values: Any existing OpenClaw home directory that already contains `openclaw.json`.

- `--backup-suffix`
  Meaning: Suffix appended to the backup of the target `openclaw.json` before import.
  Default: `.bak.migrate`
  Required: No
  Optional values: Any non-empty suffix string, such as:
  - `.bak.migrate`
  - `.bak.import`
  - `.backup.20260408`

- `--dry-run`
  Meaning: Print the import plan without modifying the target machine.
  Default: Disabled
  Required: No
  Optional values:
  - present
  - omitted

## `migrate_openclaw_agent.py`

Usage:

```bash
python3 migrate_openclaw_agent.py --source-home SOURCE_HOME --target-home TARGET_HOME --agent-id AGENT_ID [options]
```

Parameters:

- `--source-home`
  Meaning: Source OpenClaw home directory.
  Required: Yes
  Optional values: Any directory that contains `openclaw.json`.

- `--target-home`
  Meaning: Target OpenClaw home directory.
  Required: Yes
  Optional values: Any directory that contains `openclaw.json`.

- `--agent-id`
  Meaning: Agent id to migrate.
  Behavior:
  - If it is a lead agent with `subagents.allowAgents`, the whole team is migrated.
  - Otherwise only that agent is migrated.
  Required: Yes
  Optional values: Any existing agent id in the source `openclaw.json`.

- `--backup-suffix`
  Meaning: Suffix appended to the backup of the target `openclaw.json`.
  Default: `.bak.migrate`
  Required: No
  Optional values: Any non-empty suffix string.

- `--dry-run`
  Meaning: Print the migration plan without writing to the target.
  Default: Disabled
  Required: No
  Optional values:
  - present
  - omitted

## Generated Bundle Format

An exported bundle directory contains:

- `openclaw-migration.json`
  Manifest file for the bundle.
- `payload/`
  Copied agent, workspace, and related files to import.
- `migration-tools/`
  A copy of the migration entrypoints and Python bundle tool, including import
  and export scripts for macOS/Linux and Windows.

The bundle can therefore be moved to another machine and imported with the
scripts under `migration-tools/`.

When exported in the default `skeleton` mode, the bundle intentionally excludes historical runtime data from the source machine.
For example, exporting the `development` team will not include completed project history, session transcripts, or agent memory state from the source machine.

## Safety Rules

- Import does not overwrite the target machine's existing `openclaw.json`.
- Import will refuse to continue when conflicts are detected, for example:
  - same agent id already exists
  - same channel account already exists
  - same binding already exists
  - destination path already exists
- Import creates a backup of the target `openclaw.json` before writing.

## Copy-Paste Examples

These examples assume the scripts live in `/Volumes/data/liukai/tools/openclaw-migration`.

## macOS / Linux

Export a lead agent and its whole team into a new bundle directory:

```bash
/Volumes/data/liukai/tools/openclaw-migration/export-openclaw-migration.sh \
  --agent-id project-manager \
  --output-dir /Volumes/data/liukai/tools/openclaw-migration/test-bundle
```

Preview the export without writing files:

```bash
/Volumes/data/liukai/tools/openclaw-migration/export-openclaw-migration.sh \
  --agent-id project-manager \
  --output-dir /Volumes/data/liukai/tools/openclaw-migration/test-bundle \
  --dry-run
```

Explicitly export the full runtime snapshot instead of the default skeleton:

```bash
/Volumes/data/liukai/tools/openclaw-migration/export-openclaw-migration.sh \
  --agent-id project-manager \
  --output-dir /Volumes/data/liukai/tools/openclaw-migration/test-bundle-full \
  --export-mode full
```

Import a copied bundle into the current machine:

```bash
/Volumes/work/examples/ai/openclaw/import-openclaw-migration.sh \
  --input-dir /Volumes/work/examples/ai/openclaw/test \
  --target-home ~/.openclaw
```

Preview the import without changing the target machine:

```bash
/Volumes/work/examples/ai/openclaw/import-openclaw-migration.sh \
  --input-dir /Volumes/work/examples/ai/openclaw/test \
  --target-home ~/.openclaw \
  --dry-run
```

Direct one-step migration between two OpenClaw homes on the same machine:

```bash
python3 /Volumes/work/examples/ai/openclaw/migrate_openclaw_agent.py \
  --source-home ~/.openclaw \
  --target-home /tmp/other-openclaw \
  --agent-id project-manager
```

## Windows

These examples assume you copied the scripts to `D:\openclaw-tools`.

Export a lead agent and its whole team:

```bat
D:\openclaw-tools\export-openclaw-migration.bat --agent-id project-manager --output-dir D:\openclaw-bundle
```

Import a copied bundle into the target machine:

```bat
D:\openclaw-tools\import-openclaw-migration.bat --input-dir D:\openclaw-bundle --target-home C:\Users\YourName\.openclaw
```

Preview import only:

```bat
D:\openclaw-tools\import-openclaw-migration.bat --input-dir D:\openclaw-bundle --target-home C:\Users\YourName\.openclaw --dry-run
```

Direct one-step migration:

```bat
D:\openclaw-tools\migrate_openclaw_agent.bat --source-home C:\Users\YourName\.openclaw --target-home D:\other-openclaw --agent-id project-manager
```

## Notes

- Use the bundle-based flow when source and target are different machines.
- Use the direct migration tool only when both OpenClaw homes are reachable from the same machine.
- On Windows, the `.bat` wrappers require either `py -3` or `python` in `PATH`.
- On macOS and Linux, the `.sh` wrappers require `python3` in `PATH`.
