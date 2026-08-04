"""Filesystem, backup, codesign, and user-facing install messaging."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .layout import (
    AppLayout,
    PatcherError,
    app_version,
    detect_layout,
    same_path,
    sha256_bytes,
    sha256_file,
)
from .patching import BundlePlan

GREEN = "\033[1;32m"
RESET = "\033[0m"

SUCCESS_INSTRUCTIONS_MACOS = (
    "Next steps:\n"
    "  1. Open the fixed app. macOS/Cursor will warn that its installation "
    "files were modified; this is expected.\n"
    '  2. When prompted for "Cursor Safe Storage", authorize access and choose '
    '"Always Allow".\n'
    "  3. The first startup may report a shell environment timeout if that "
    "prompt blocked startup.\n"
    "  4. After granting access, fully quit the fixed app and open it again; "
    "the timeout should be resolved.\n"
    '  5. Cursor may show "Your Cursor installation appears to be corrupt. '
    'Please reinstall" (or mark the window as Unsupported). That comes from '
    "Cursor's integrity check of patched files and can appear on macOS or "
    "Windows; it is safe to ignore. Do not reinstall solely because of it. "
    "Some machines never show the banner.\n"
    "  6. Optional: replace your usual Cursor shortcut/Dock icon with the "
    "fixed app path so everyday launches use the patched build.\n"
    "\n"
    "Clone mode leaves /Applications/Cursor.app untouched. The clone shares "
    "your existing Cursor settings, extensions, conversations, and API keys; "
    "it does not create or modify a separate global configuration."
)

SUCCESS_INSTRUCTIONS_WINDOWS = (
    "Next steps:\n"
    "  1. Open the fixed Cursor.exe. Cursor may warn that its installation "
    "files were modified; this is expected.\n"
    '  2. Cursor may show "Your Cursor installation appears to be corrupt. '
    'Please reinstall" (or mark the window as Unsupported). That comes from '
    "Cursor's integrity check of patched files and can appear on macOS or "
    "Windows; it is safe to ignore. Do not reinstall solely because of it. "
    "Some machines never show the banner.\n"
    "  3. Optional: replace your Start Menu / taskbar / desktop Cursor "
    "shortcut so it points at this fixed install path for convenience.\n"
    "  4. Prefer a per-user install under Local\\Programs\\cursor for patching; "
    "Program Files installs often need elevated permissions to modify.\n"
    "\n"
    "Clone mode leaves the original Cursor install untouched. The clone shares "
    "your existing Cursor settings, extensions, conversations, and API keys; "
    "it does not create or modify a separate global configuration."
)


def success_instructions(layout: AppLayout) -> str:
    if layout.kind == "windows":
        return SUCCESS_INSTRUCTIONS_WINDOWS
    return SUCCESS_INSTRUCTIONS_MACOS


def ensure_writable(path: Path) -> None:
    if not path.exists():
        return
    mode = path.stat().st_mode
    if not mode & stat.S_IWRITE:
        path.chmod(mode | stat.S_IWRITE)


def atomic_write(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode if path.exists() else 0o644
    ensure_writable(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode | stat.S_IWRITE)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_json_atomic(path: Path, value: dict) -> None:
    atomic_write(
        path, (json.dumps(value, indent=2) + "\n").encode("utf-8")
    )


def clone_app(source: Path, output: Path, force: bool) -> None:
    if output.exists():
        if not force:
            raise PatcherError(f"Output already exists: {output} (use --force-output)")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    layout = detect_layout(source)
    if layout.kind == "macos":
        subprocess.run(["ditto", str(source), str(output)], check=True)
        return
    shutil.copytree(source, output, symlinks=True)


def backup_plans(app: Path, plans: list[BundlePlan], backup_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"cursor-{app_version(app)}-{stamp}"
    suffix = 2
    while backup.exists():
        backup = backup_root / f"cursor-{app_version(app)}-{stamp}-{suffix}"
        suffix += 1
    backup.mkdir(parents=True)
    files = []
    for plan in plans:
        relative = plan.path.relative_to(app)
        destination = backup / "files" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = plan.original.encode("utf-8")
        destination.write_bytes(data)
        files.append(
            {
                "relative_path": relative.as_posix(),
                "original_sha256": sha256_bytes(data),
                "patched_sha256": sha256_bytes(plan.patched.encode("utf-8")),
            }
        )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_path": str(app),
        "cursor_version": app_version(app),
        "layout": detect_layout(app).kind,
        "state": "prepared",
        "files": files,
    }
    write_json_atomic(backup / "manifest.json", manifest)
    return backup


def sign_app(app: Path) -> None:
    layout = detect_layout(app)
    if not layout.codesign:
        return
    executable = app / "Contents" / "MacOS" / plist_executable(app)
    entitlements = extract_entitlements(executable)
    command = [
        "codesign",
        "--force",
        "--deep",
        "--sign",
        "-",
    ]
    temporary_entitlements: str | None = None
    try:
        if entitlements:
            fd, temporary_entitlements = tempfile.mkstemp(
                prefix="cursor-entitlements-", suffix=".plist"
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(entitlements)
            command.extend(["--entitlements", temporary_entitlements])
        command.append(str(app))
        subprocess.run(command, check=True)
    finally:
        if temporary_entitlements is not None:
            try:
                os.unlink(temporary_entitlements)
            except FileNotFoundError:
                pass
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def plist_executable(app: Path) -> str:
    plist = app / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as handle:
            value = plistlib.load(handle).get("CFBundleExecutable")
    except (OSError, plistlib.InvalidFileException) as error:
        raise PatcherError(f"Unable to read app executable from {plist}: {error}") from error
    if not isinstance(value, str) or not value:
        raise PatcherError(f"CFBundleExecutable is missing from {plist}")
    return value


def extract_entitlements(executable: Path) -> bytes | None:
    result = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(executable)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    output = result.stdout + b"\n" + result.stderr
    start = output.find(b"<?xml")
    end = output.rfind(b"</plist>")
    if start == -1 or end == -1:
        return None
    entitlements = output[start : end + len(b"</plist>")]
    try:
        plistlib.loads(entitlements)
    except plistlib.InvalidFileException as error:
        raise PatcherError(f"Invalid entitlements from {executable}: {error}") from error
    return entitlements


def load_manifest(backup: Path) -> dict:
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file():
        raise PatcherError(f"Backup manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def update_backup_state(backup: Path, state: str, error: str | None = None) -> None:
    manifest = load_manifest(backup)
    manifest["state"] = state
    if error is not None:
        manifest["error"] = error
    write_json_atomic(backup / "manifest.json", manifest)


def restore_plans_from_backup(app: Path, backup: Path) -> None:
    manifest = load_manifest(backup)
    for entry in manifest.get("files", []):
        relative = Path(entry["relative_path"])
        source = backup / "files" / relative
        data = source.read_bytes()
        if sha256_bytes(data) != entry["original_sha256"]:
            raise PatcherError(f"Backup checksum mismatch during rollback: {source}")
        atomic_write(app / relative, data)


def latest_backup(backup_root: Path, app: Path) -> Path:
    candidates = []
    if backup_root.is_dir():
        for child in backup_root.iterdir():
            if not child.is_dir() or not (child / "manifest.json").is_file():
                continue
            try:
                manifest = load_manifest(child)
            except (OSError, json.JSONDecodeError):
                continue
            if same_path(Path(manifest.get("app_path", "")).expanduser(), app):
                candidates.append(child)
    if not candidates:
        raise PatcherError(f"No backup found for {app} under {backup_root}")
    return sorted(candidates)[-1]


def color_enabled(stream: object = sys.stdout) -> bool:
    return (
        "NO_COLOR" not in os.environ
        and bool(getattr(stream, "isatty", lambda: False)())
    )


def print_patch_success(target: Path, backup: Path, stream: object = sys.stdout) -> None:
    heading = "✓ Cursor OpenAI Routing Fix installed successfully"
    if color_enabled(stream):
        heading = f"{GREEN}{heading}{RESET}"
    print(heading, file=stream)
    print(f"Fixed app: {target}", file=stream)
    print(f"Backup: {backup}", file=stream)
    print(file=stream)
    try:
        instructions = success_instructions(detect_layout(target))
    except PatcherError:
        instructions = SUCCESS_INSTRUCTIONS_MACOS
    print(instructions, file=stream)
