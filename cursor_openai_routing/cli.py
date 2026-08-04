"""CLI entry points for patch / restore / status."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .layout import (
    DEFAULT_APP,
    DEFAULT_BACKUP_ROOT,
    DEFAULT_OUTPUT,
    PatcherError,
    app_version,
    detect_layout,
    discover_bundles,
    is_wsl,
    same_path,
    sha256_bytes,
    sha256_file,
)
from .ops import (
    atomic_write,
    backup_plans,
    clone_app,
    latest_backup,
    load_manifest,
    print_patch_success,
    restore_plans_from_backup,
    sign_app,
    update_backup_state,
)
from .patching import plan_bundle


def command_patch(args: argparse.Namespace) -> None:
    source = args.app.expanduser().resolve()
    if args.in_place and args.output:
        raise PatcherError("--output and --in-place cannot be used together")
    target = (
        source
        if args.in_place
        else (args.output or DEFAULT_OUTPUT).expanduser().resolve()
    )
    if not args.in_place and source == target:
        raise PatcherError("Source and output are identical; use --in-place explicitly")

    source_plans = [
        plan_bundle(path, args.trace) for path in discover_bundles(source)
    ]
    source_needs_patch = [
        plan for plan in source_plans if plan.state == "needs-patch"
    ]
    for plan in source_plans:
        print(f"{plan.path.name}: {plan.state}")
    if not source_needs_patch:
        print("Nothing to patch.")
        return
    if args.dry_run:
        print(
            f"Dry run: {len(source_needs_patch)} bundle(s) would be patched in {target}"
        )
        return

    if not args.in_place:
        clone_app(source, target, args.force_output)
        plans = [plan_bundle(path, args.trace) for path in discover_bundles(target)]
    else:
        plans = source_plans
    needs_patch = [plan for plan in plans if plan.state == "needs-patch"]
    backup = backup_plans(target, needs_patch, args.backup_root.expanduser())
    try:
        for plan in needs_patch:
            if sha256_file(plan.path) != sha256_bytes(plan.original.encode("utf-8")):
                raise PatcherError(f"{plan.path.name}: changed after patch planning")
            atomic_write(plan.path, plan.patched.encode("utf-8"))
        sign_app(target)
        update_backup_state(backup, "committed")
    except Exception as error:
        try:
            restore_plans_from_backup(target, backup)
            sign_app(target)
            update_backup_state(backup, "rolled-back", str(error))
        except Exception as rollback_error:
            update_backup_state(
                backup,
                "recovery-required",
                f"patch error: {error}; rollback error: {rollback_error}",
            )
            raise PatcherError(
                f"Patch failed and automatic rollback was incomplete. "
                f"Recover manually from {backup}: {rollback_error}"
            ) from error
        raise PatcherError(f"Patch failed; original payloads restored: {error}") from error
    print_patch_success(target, backup)


def command_restore(args: argparse.Namespace) -> None:
    app = args.app.expanduser().resolve()
    backup = (
        args.backup.expanduser().resolve()
        if args.backup
        else latest_backup(args.backup_root.expanduser(), app)
    )
    manifest = load_manifest(backup)
    recorded_app = Path(manifest.get("app_path", "")).expanduser()
    if not same_path(recorded_app, app):
        raise PatcherError(
            f"Backup belongs to {recorded_app}, not {app}"
        )
    if manifest.get("state") not in {"committed", "prepared", "rolled-back"}:
        raise PatcherError(
            f"Backup is not safely restorable (state={manifest.get('state')!r})"
        )
    restored = 0
    for entry in manifest.get("files", []):
        relative = Path(entry["relative_path"])
        source = backup / "files" / relative
        destination = app / relative
        if not source.is_file():
            raise PatcherError(f"Backup file missing: {source}")
        data = source.read_bytes()
        if sha256_bytes(data) != entry["original_sha256"]:
            raise PatcherError(f"Backup checksum mismatch: {source}")
        if not destination.is_file():
            raise PatcherError(f"Target file missing: {destination}")
        current_hash = sha256_file(destination)
        allowed_hashes = {
            entry["original_sha256"],
            entry["patched_sha256"],
        }
        if current_hash not in allowed_hashes:
            raise PatcherError(
                f"Refusing to overwrite changed or updated file: {destination}"
            )
        if not args.dry_run:
            atomic_write(destination, data)
        restored += 1
        print(f"{relative.as_posix()}: {'would restore' if args.dry_run else 'restored'}")
    if restored == 0:
        raise PatcherError(f"Backup contains no files: {backup}")
    if not args.dry_run:
        sign_app(app)
    print(f"{'Would restore from' if args.dry_run else 'Restored from'}: {backup}")


def command_status(args: argparse.Namespace) -> None:
    app = args.app.expanduser().resolve()
    print(f"App: {app}")
    print(f"Version: {app_version(app)}")
    try:
        print(f"Layout: {detect_layout(app).kind}")
    except PatcherError as error:
        raise PatcherError(str(error)) from error
    bad = False
    for path in discover_bundles(app):
        try:
            plan = plan_bundle(path, trace=False)
            state = plan.state
        except PatcherError as error:
            state = f"unsupported ({error})"
            bad = True
        print(f"{path.name}: {state}")
    if bad:
        raise PatcherError("One or more bundles are unsupported")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely patch Cursor for per-model OpenAI API key routing "
            "on macOS and Windows."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    patch = subparsers.add_parser("patch", help="clone and patch Cursor")
    patch.add_argument("--app", type=Path, default=DEFAULT_APP, help="source Cursor install")
    patch.add_argument("--output", type=Path, help=f"clone destination (default: {DEFAULT_OUTPUT})")
    patch.add_argument("--in-place", action="store_true", help="patch --app directly")
    patch.add_argument("--force-output", action="store_true", help="replace an existing output clone")
    patch.add_argument("--dry-run", action="store_true", help="inspect without copying or writing")
    patch.add_argument("--trace", action="store_true", help="log key-safe routing booleans")
    patch.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    patch.set_defaults(handler=command_patch)

    restore = subparsers.add_parser("restore", help="restore files from a backup")
    restore.add_argument("--app", type=Path, default=DEFAULT_OUTPUT, help="app to restore")
    restore.add_argument("--backup", type=Path, help="specific backup directory")
    restore.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    restore.add_argument("--dry-run", action="store_true")
    restore.set_defaults(handler=command_restore)

    status = subparsers.add_parser("status", help="inspect patch compatibility/state")
    status.add_argument("--app", type=Path, default=DEFAULT_APP)
    status.set_defaults(handler=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform not in {"darwin", "win32"} and not is_wsl():
        print(
            "warning: this host is neither macOS, Windows, nor WSL; "
            "pass --app pointing at a Cursor install with a recognized layout",
            file=sys.stderr,
        )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
        return 0
    except (PatcherError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
