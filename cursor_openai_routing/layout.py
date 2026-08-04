"""Host paths, Cursor install layout detection, and bundle discovery."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path


class PatcherError(RuntimeError):
    pass


SUPPORTED_BUNDLES = {"workbench.desktop.main.js", "workbench.glass.main.js"}


@dataclass(frozen=True)
class AppLayout:
    kind: str
    bundle_glob: str
    codesign: bool


MACOS_LAYOUT = AppLayout(
    kind="macos",
    bundle_glob="Contents/Resources/app/out/vs/workbench/workbench.*.main.js",
    codesign=True,
)
WINDOWS_LAYOUT = AppLayout(
    kind="windows",
    bundle_glob="resources/app/out/vs/workbench/workbench.*.main.js",
    codesign=False,
)


@dataclass(frozen=True)
class HostDefaults:
    app: Path
    output: Path
    backup_root: Path


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def windows_local_app_data() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local)
        return Path.home() / "AppData" / "Local"
    profile = os.environ.get("USERPROFILE")
    if profile and Path(profile).is_dir():
        return Path(profile) / "AppData" / "Local"
    # WSL: map the current Linux username to the Windows profile when possible.
    candidates = [
        Path("/mnt/c/Users") / Path.home().name / "AppData" / "Local",
        Path("/mnt/c/Users") / os.environ.get("USER", "") / "AppData" / "Local",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    users = Path("/mnt/c/Users")
    if users.is_dir():
        for child in sorted(users.iterdir()):
            local = child / "AppData" / "Local"
            if local.is_dir() and child.name not in {"Public", "Default", "Default User", "All Users"}:
                return local
    return Path.home() / "AppData" / "Local"


def windows_program_files() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    return Path("/mnt/c/Program Files")


def windows_app_candidates() -> list[Path]:
    local = windows_local_app_data()
    return [
        local / "Programs" / "cursor",
        local / "Programs" / "Cursor",
        windows_program_files() / "cursor",
        windows_program_files() / "Cursor",
    ]


def resolve_existing_app(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def host_defaults() -> HostDefaults:
    if sys.platform == "darwin":
        return HostDefaults(
            app=Path("/Applications/Cursor.app"),
            output=Path.home() / "Applications" / "Cursor OpenAI Routing Fix.app",
            backup_root=(
                Path.home()
                / "Library"
                / "Application Support"
                / "Cursor OpenAI Routing Fix"
                / "backups"
            ),
        )
    if sys.platform == "win32" or is_wsl():
        local = windows_local_app_data()
        app = resolve_existing_app(windows_app_candidates()) or (
            local / "Programs" / "cursor"
        )
        return HostDefaults(
            app=app,
            output=local / "Programs" / "Cursor OpenAI Routing Fix",
            backup_root=local / "Cursor OpenAI Routing Fix" / "backups",
        )
    return HostDefaults(
        app=Path("/Applications/Cursor.app"),
        output=Path.home() / "Cursor OpenAI Routing Fix",
        backup_root=Path.home() / ".local" / "share" / "cursor-openai-routing-fix" / "backups",
    )


DEFAULTS = host_defaults()
DEFAULT_APP = DEFAULTS.app
DEFAULT_OUTPUT = DEFAULTS.output
DEFAULT_BACKUP_ROOT = DEFAULTS.backup_root


def detect_layout(app: Path) -> AppLayout:
    if (app / "Contents" / "Resources" / "app").is_dir():
        return MACOS_LAYOUT
    if (app / "resources" / "app").is_dir():
        return WINDOWS_LAYOUT
    raise PatcherError(
        f"Unrecognized Cursor layout at {app}. Expected a macOS .app bundle "
        f"(Contents/Resources/app) or a Windows install (resources/app)."
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left.resolve() == right.resolve()


def app_version(app: Path) -> str:
    try:
        layout = detect_layout(app)
    except PatcherError:
        return "unknown"
    if layout.kind == "macos":
        plist = app / "Contents" / "Info.plist"
        try:
            with plist.open("rb") as handle:
                return str(
                    plistlib.load(handle).get("CFBundleShortVersionString", "unknown")
                )
        except (OSError, plistlib.InvalidFileException):
            return "unknown"
    for relative in (
        Path("resources/app/product.json"),
        Path("resources/app/package.json"),
    ):
        path = app / relative
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        version = payload.get("version")
        if isinstance(version, str) and version:
            return version
    return "unknown"


def discover_bundles(app: Path) -> list[Path]:
    if not app.is_dir():
        raise PatcherError(f"Application not found: {app}")
    layout = detect_layout(app)
    bundles = [
        path
        for path in sorted(app.glob(layout.bundle_glob))
        if path.name in SUPPORTED_BUNDLES
    ]
    if not bundles:
        raise PatcherError(f"No supported workbench bundles found in {app}")
    return bundles
