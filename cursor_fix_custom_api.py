#!/usr/bin/env python3
"""Patch Cursor's macOS workbench bundles for per-model BYOK routing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Match


DEFAULT_APP = Path("/Applications/Cursor.app")
DEFAULT_OUTPUT = Path.home() / "Applications" / "Cursor Custom API Fix.app"
DEFAULT_BACKUP_ROOT = (
    Path.home() / "Library" / "Application Support" / "Cursor Custom API Fix" / "backups"
)
BUNDLE_GLOB = "Contents/Resources/app/out/vs/workbench/workbench.*.main.js"
SUPPORTED_BUNDLES = {"workbench.desktop.main.js", "workbench.glass.main.js"}
PATCH_MARKER = "[cursor-fix-custom-api]"
GREEN = "\033[1;32m"
RESET = "\033[0m"
SUCCESS_INSTRUCTIONS = (
    "Next steps:\n"
    "  1. Open the fixed app. macOS/Cursor will warn that its installation "
    "files were modified; this is expected.\n"
    '  2. When prompted for "Cursor Safe Storage", authorize access and choose '
    '"Always Allow".\n'
    "  3. The first startup may report a shell environment timeout if that "
    "prompt blocked startup.\n"
    "  4. After granting access, fully quit the fixed app and open it again; "
    "the timeout should be resolved.\n"
    "\n"
    "Clone mode leaves /Applications/Cursor.app untouched. The clone shares "
    "your existing Cursor settings, extensions, conversations, and API keys; "
    "it does not create or modify a separate global configuration."
)
OPENAI_MODEL_LITERAL = r"/^(?:gpt(?:-|$)|chatgpt(?:-|$)|o[134](?:-|$)|codex(?:-|$))/"
OPENAI_MODEL_PATTERN_RE = re.escape(OPENAI_MODEL_LITERAL)

IDENT = r"[$A-Za-z_][$\w]*"

CLASSIFIER_RE = re.compile(
    rf"""
    function[ ](?P<function>{IDENT})\(
      (?P<model>{IDENT}),(?P<settings>{IDENT})
    \)\{{return[ ]
      (?P<claude>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useClaudeKey\?"anthropic":void[ ]0:
      (?P<google>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useGoogleKey\?"google":void[ ]0:
      (?P=settings)\.useOpenAIKey\?"openai":void[ ]0
    \}}
    """,
    re.VERBOSE,
)

PATCHED_CLASSIFIER_RE = re.compile(
    rf"""
    function[ ](?P<function>{IDENT})\(
      (?P<model>{IDENT}),(?P<settings>{IDENT})
    \)\{{return[ ]
      (?P<claude>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useClaudeKey\?"anthropic":void[ ]0:
      (?P<google>{IDENT})\((?P=model)\)
      \?(?P=settings)\.useGoogleKey\?"google":void[ ]0:
      (?P=settings)\.useOpenAIKey&&
      \(\((?P=settings)\.aiSettings\?\.userAddedModels\?\.includes\((?P=model)\)\?\?!1\)
      \|\|{OPENAI_MODEL_PATTERN_RE}\.test\((?P=model)\)\)
      \?"openai":void[ ]0
    \}}
    """,
    re.VERBOSE,
)

MODEL_DETAILS_RE = re.compile(
    rf"""
    getModelDetailsFromName\(
      (?P<model>{IDENT}),(?P<max>{IDENT})
    \)\{{let[ ](?P<key>{IDENT})=
      this\._cursorAuthenticationService\.getApiKeyForModel\((?P=model)\);
    const[ ](?P<enabled>{IDENT})=
      this\._aiSettingsService\.getUseApiKeyForModel\((?P=model)\),
      (?P<azure>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.azureState,
      (?P<bedrock>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.bedrockState;
    \(!(?P=enabled)\|\|!(?P=key)\)&&\((?P=key)=void[ ]0\);
    const[ ](?P<server>{IDENT})=
      this\._aiSettingsService\.getServerModelName\((?P=model)\);
    return[ ]new[ ](?P<ctor>{IDENT})\(\{{apiKey:(?P=key),
      modelName:(?P=server),azureState:(?P=azure),
      openaiApiBaseUrl:
      this\._reactiveStorageService\.applicationUserPersistentStorage\.openAIBaseUrl
      \?\?void[ ]0,
      bedrockState:(?P=bedrock),maxMode:(?P=max)\}}\)
    \}}
    """,
    re.VERBOSE,
)

PATCHED_MODEL_DETAILS_RE = re.compile(
    rf"""
    getModelDetailsFromName\(
      (?P<model>{IDENT}),(?P<max>{IDENT})
    \)\{{let[ ](?P<key>{IDENT})=
      this\._cursorAuthenticationService\.getApiKeyForModel\((?P=model)\);
    const[ ](?P<enabled>{IDENT})=
      this\._aiSettingsService\.getUseApiKeyForModel\((?P=model)\),
      (?P<azure>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.azureState,
      (?P<bedrock>{IDENT})=
      this\._reactiveStorageService\.applicationUserPersistentStorage\.bedrockState;
    \(!(?P=enabled)\|\|!(?P=key)\)&&\((?P=key)=void[ ]0\);
    const[ ](?P<server>{IDENT})=
      this\._aiSettingsService\.getServerModelName\((?P=model)\),
      (?P<base>{IDENT})=(?P=key)\?
      this\._reactiveStorageService\.applicationUserPersistentStorage\.openAIBaseUrl
      \?\?void[ ]0:void[ ]0
      (?:,(?P<provider_var>{IDENT})=(?P<provider_fn>{IDENT})\(
        (?P=model),this\._reactiveStorageService\.applicationUserPersistentStorage
      \))?;
    return[ ](?:console\.info\("{re.escape(PATCH_MARKER)}"[^\n]*?\),)?
      new[ ](?P<ctor>{IDENT})\(\{{apiKey:(?P=key),
      modelName:(?P=server),azureState:(?P=azure),
      openaiApiBaseUrl:(?P=base),
      bedrockState:(?P=bedrock),maxMode:(?P=max)\}}\)
    \}}
    """,
    re.VERBOSE,
)


class PatcherError(RuntimeError):
    pass


@dataclass
class BundlePlan:
    path: Path
    original: str
    patched: str
    state: str


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
    plist = app / "Contents" / "Info.plist"
    try:
        with plist.open("rb") as handle:
            return str(plistlib.load(handle).get("CFBundleShortVersionString", "unknown"))
    except (OSError, plistlib.InvalidFileException):
        return "unknown"


def discover_bundles(app: Path) -> list[Path]:
    if not app.is_dir():
        raise PatcherError(f"Application not found: {app}")
    bundles = [
        path
        for path in sorted(app.glob(BUNDLE_GLOB))
        if path.name in SUPPORTED_BUNDLES
    ]
    if not bundles:
        raise PatcherError(f"No supported workbench bundles found in {app}")
    return bundles


def classifier_replacement(match: Match[str]) -> str:
    g = match.groupdict()
    model, settings = g["model"], g["settings"]
    return (
        f'function {g["function"]}({model},{settings}){{return '
        f'{g["claude"]}({model})?{settings}.useClaudeKey?"anthropic":void 0:'
        f'{g["google"]}({model})?{settings}.useGoogleKey?"google":void 0:'
        f"{settings}.useOpenAIKey&&"
        f"(({settings}.aiSettings?.userAddedModels?.includes({model})??!1)||"
        f"{OPENAI_MODEL_LITERAL}.test({model}))?"
        f'"openai":void 0}}'
    )


def model_details_replacement(match: Match[str], trace: bool) -> str:
    g = match.groupdict()
    model, key = g["model"], g["key"]
    base = unique_identifier(match.string, "cursorFixBase")
    provider = unique_identifier(match.string, "cursorFixProvider")
    prefix = (
        f"getModelDetailsFromName({model},{g['max']}){{let {key}="
        f"this._cursorAuthenticationService.getApiKeyForModel({model});"
        f"const {g['enabled']}=this._aiSettingsService.getUseApiKeyForModel({model}),"
        f"{g['azure']}=this._reactiveStorageService.applicationUserPersistentStorage.azureState,"
        f"{g['bedrock']}=this._reactiveStorageService.applicationUserPersistentStorage.bedrockState;"
        f"(!{g['enabled']}||!{key})&&({key}=void 0);"
        f"const {g['server']}=this._aiSettingsService.getServerModelName({model}),"
        f"{base}={key}?this._reactiveStorageService.applicationUserPersistentStorage."
        f"openAIBaseUrl??void 0:void 0"
    )
    if trace:
        prefix += (
            f",{provider}={g['function'] if 'function' in g else 'undefined'}"
            if False
            else ""
        )
        trace_code = (
            f'console.info("{PATCH_MARKER}",{{modelName:{model},'
            f"apiKeyAttached:!!{key},openaiApiBaseUrlAttached:!!{base}}}),"
        )
    else:
        trace_code = ""
    return (
        prefix
        + ";return "
        + trace_code
        + f"new {g['ctor']}({{apiKey:{key},modelName:{g['server']},"
        f"azureState:{g['azure']},openaiApiBaseUrl:{base},"
        f"bedrockState:{g['bedrock']},maxMode:{g['max']}}})}}"
    )


def unique_identifier(text: str, stem: str) -> str:
    candidate = stem
    number = 2
    while re.search(rf"(?<![$\w]){re.escape(candidate)}(?![$\w])", text):
        candidate = f"{stem}{number}"
        number += 1
    return candidate


def plan_bundle(path: Path, trace: bool) -> BundlePlan:
    text = path.read_text(encoding="utf-8")
    vulnerable_classifiers = list(CLASSIFIER_RE.finditer(text))
    patched_classifiers = list(PATCHED_CLASSIFIER_RE.finditer(text))
    vulnerable_details = list(MODEL_DETAILS_RE.finditer(text))
    patched_details = list(PATCHED_MODEL_DETAILS_RE.finditer(text))

    if not vulnerable_classifiers and not vulnerable_details:
        if len(patched_classifiers) == 1 and len(patched_details) == 1:
            return BundlePlan(path, text, text, "already-patched")
        raise PatcherError(
            f"{path.name}: unsupported or ambiguous bundle "
            f"(classifier vulnerable={len(vulnerable_classifiers)}, "
            f"patched={len(patched_classifiers)}; model-details "
            f"vulnerable={len(vulnerable_details)}, patched={len(patched_details)})"
        )
    if len(vulnerable_classifiers) != 1 or len(vulnerable_details) != 1:
        raise PatcherError(
            f"{path.name}: refusing partial/ambiguous patch "
            f"(classifier={len(vulnerable_classifiers)}, "
            f"model-details={len(vulnerable_details)})"
        )

    patched = CLASSIFIER_RE.sub(classifier_replacement, text, count=1)
    patched = MODEL_DETAILS_RE.sub(
        lambda match: model_details_replacement(match, trace), patched, count=1
    )
    if patched == text:
        raise PatcherError(f"{path.name}: patch unexpectedly made no changes")
    if len(PATCHED_CLASSIFIER_RE.findall(patched)) != 1:
        raise PatcherError(f"{path.name}: patched classifier failed validation")
    if len(PATCHED_MODEL_DETAILS_RE.findall(patched)) != 1:
        raise PatcherError(f"{path.name}: patched model-details failed validation")
    return BundlePlan(path, text, patched, "needs-patch")


def atomic_write(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
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
    subprocess.run(["ditto", str(source), str(output)], check=True)


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
                "relative_path": str(relative),
                "original_sha256": sha256_bytes(data),
                "patched_sha256": sha256_bytes(plan.patched.encode("utf-8")),
            }
        )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_path": str(app),
        "cursor_version": app_version(app),
        "state": "prepared",
        "files": files,
    }
    write_json_atomic(backup / "manifest.json", manifest)
    return backup


def sign_app(app: Path) -> None:
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


def color_enabled(stream: object = sys.stdout) -> bool:
    return (
        "NO_COLOR" not in os.environ
        and bool(getattr(stream, "isatty", lambda: False)())
    )


def print_patch_success(target: Path, backup: Path, stream: object = sys.stdout) -> None:
    heading = "✓ Cursor Custom API Fix installed successfully"
    if color_enabled(stream):
        heading = f"{GREEN}{heading}{RESET}"
    print(heading, file=stream)
    print(f"Fixed app: {target}", file=stream)
    print(f"Backup: {backup}", file=stream)
    print(file=stream)
    print(SUCCESS_INSTRUCTIONS, file=stream)


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


def load_manifest(backup: Path) -> dict:
    manifest_path = backup / "manifest.json"
    if not manifest_path.is_file():
        raise PatcherError(f"Backup manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


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
        print(f"{relative}: {'would restore' if args.dry_run else 'restored'}")
    if restored == 0:
        raise PatcherError(f"Backup contains no files: {backup}")
    if not args.dry_run:
        sign_app(app)
    print(f"{'Would restore from' if args.dry_run else 'Restored from'}: {backup}")


def command_status(args: argparse.Namespace) -> None:
    app = args.app.expanduser().resolve()
    print(f"App: {app}")
    print(f"Version: {app_version(app)}")
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
        description="Safely patch Cursor for per-model custom API key routing on macOS."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    patch = subparsers.add_parser("patch", help="clone and patch Cursor")
    patch.add_argument("--app", type=Path, default=DEFAULT_APP, help="source Cursor.app")
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
    if sys.platform != "darwin":
        print("error: this version supports macOS only", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
        return 0
    except (PatcherError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
