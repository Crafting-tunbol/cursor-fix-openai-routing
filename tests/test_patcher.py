import io
import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cursor_fix_openai_routing as patcher


class Output(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def vulnerable_bundle(
    classifier="function vap(t,e)",
    claude="RB_",
    google="PB_",
    ctor="hg",
) -> str:
    function_name = classifier.removeprefix("function ").split("(", 1)[0]
    model, settings = classifier.split("(", 1)[1].removesuffix(")").split(",")
    return (
        f'prefix;function {function_name}({model},{settings}){{return '
        f'{claude}({model})?{settings}.useClaudeKey?"anthropic":void 0:'
        f'{google}({model})?{settings}.useGoogleKey?"google":void 0:'
        f'{settings}.useOpenAIKey?"openai":void 0}};middle;'
        f"getModelDetailsFromName({model},x){{let n=this._cursorAuthenticationService."
        f"getApiKeyForModel({model});const i=this._aiSettingsService."
        f"getUseApiKeyForModel({model}),r=this._reactiveStorageService."
        "applicationUserPersistentStorage.azureState,s=this._reactiveStorageService."
        "applicationUserPersistentStorage.bedrockState;(!i||!n)&&(n=void 0);"
        f"const o=this._aiSettingsService.getServerModelName({model});"
        f"return new {ctor}({{apiKey:n,modelName:o,azureState:r,"
        "openaiApiBaseUrl:this._reactiveStorageService.applicationUserPersistentStorage."
        "openAIBaseUrl??void 0,bedrockState:s,maxMode:x})};suffix"
    )


class PatcherTests(unittest.TestCase):
    def test_success_instructions_use_color_on_tty(self):
        output = Output(tty=True)
        with patch.dict(os.environ, {}, clear=True):
            patcher.print_patch_success(
                Path("/tmp/Fixed.app"), Path("/tmp/backup"), output
            )
        value = output.getvalue()
        self.assertIn(patcher.GREEN, value)
        self.assertIn("Always Allow", value)
        self.assertIn("shell environment timeout", value)
        self.assertIn("appears to be corrupt", value)
        self.assertIn("/Applications/Cursor.app untouched", value)

    def test_success_instructions_disable_color_for_non_tty_or_no_color(self):
        for tty, environment in ((False, {}), (True, {"NO_COLOR": "1"})):
            with self.subTest(tty=tty, environment=environment):
                output = Output(tty=tty)
                with patch.dict(os.environ, environment, clear=True):
                    patcher.print_patch_success(
                        Path("/tmp/Fixed.app"), Path("/tmp/backup"), output
                    )
                self.assertNotIn("\033[", output.getvalue())

    def test_dry_run_does_not_print_success(self):
        args = type(
            "Args",
            (),
            {
                "app": Path("/tmp/Cursor.app"),
                "output": Path("/tmp/Fixed.app"),
                "in_place": False,
                "force_output": False,
                "dry_run": True,
                "trace": False,
                "backup_root": Path("/tmp/backups"),
            },
        )()
        plan = patcher.BundlePlan(
            Path("/tmp/workbench.desktop.main.js"), "original", "patched", "needs-patch"
        )
        output = io.StringIO()
        with (
            patch.object(patcher, "discover_bundles", return_value=[plan.path]),
            patch.object(patcher, "plan_bundle", return_value=plan),
            patch("sys.stdout", output),
        ):
            patcher.command_patch(args)
        self.assertIn("Dry run:", output.getvalue())
        self.assertNotIn("installed successfully", output.getvalue())

    def test_semantic_patch_handles_changed_minified_names(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "workbench.glass.main.js"
            bundle.write_text(
                vulnerable_bundle("function z9($m,_s)", "ca", "gg", "$C"),
                encoding="utf-8",
            )
            plan = patcher.plan_bundle(bundle, trace=True)
            self.assertEqual(plan.state, "needs-patch")
            self.assertIn("_s.aiSettings?.userAddedModels?.includes($m)", plan.patched)
            self.assertIn(patcher.OPENAI_MODEL_LITERAL, plan.patched)
            self.assertIn("openaiApiBaseUrl:cursorFixBase", plan.patched)
            self.assertIn(patcher.PATCH_MARKER, plan.patched)
            bundle.write_text(plan.patched, encoding="utf-8")
            self.assertEqual(
                patcher.plan_bundle(bundle, trace=False).state, "already-patched"
            )

    def test_ambiguous_classifier_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "workbench.desktop.main.js"
            text = vulnerable_bundle()
            classifier = patcher.CLASSIFIER_RE.search(text).group(0)
            bundle.write_text(text + classifier, encoding="utf-8")
            with self.assertRaisesRegex(patcher.PatcherError, "ambiguous"):
                patcher.plan_bundle(bundle, trace=False)

    def test_backup_and_restore_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Cursor Test.app"
            workbench = (
                app
                / "Contents"
                / "Resources"
                / "app"
                / "out"
                / "vs"
                / "workbench"
            )
            workbench.mkdir(parents=True)
            info = app / "Contents" / "Info.plist"
            with info.open("wb") as handle:
                plistlib.dump({"CFBundleShortVersionString": "9.9.9"}, handle)
            bundle = workbench / "workbench.desktop.main.js"
            original = vulnerable_bundle()
            bundle.write_text(original, encoding="utf-8")
            plan = patcher.plan_bundle(bundle, trace=False)
            backup_root = root / "backups"
            backup = patcher.backup_plans(app, [plan], backup_root)
            patcher.atomic_write(bundle, plan.patched.encode())

            args = type(
                "Args",
                (),
                {
                    "app": app,
                    "backup": backup,
                    "backup_root": backup_root,
                    "dry_run": False,
                },
            )()
            with patch.object(patcher, "sign_app"):
                patcher.command_restore(args)
            self.assertEqual(bundle.read_text(encoding="utf-8"), original)
            manifest = json.loads((backup / "manifest.json").read_text())
            self.assertEqual(
                manifest["files"][0]["original_sha256"],
                patcher.sha256_bytes(original.encode()),
            )

    def test_restore_refuses_file_changed_after_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Cursor Test.app"
            workbench = (
                app
                / "Contents"
                / "Resources"
                / "app"
                / "out"
                / "vs"
                / "workbench"
            )
            workbench.mkdir(parents=True)
            info = app / "Contents" / "Info.plist"
            with info.open("wb") as handle:
                plistlib.dump({"CFBundleShortVersionString": "9.9.9"}, handle)
            bundle = workbench / "workbench.desktop.main.js"
            bundle.write_text(vulnerable_bundle(), encoding="utf-8")
            plan = patcher.plan_bundle(bundle, trace=False)
            backup_root = root / "backups"
            backup = patcher.backup_plans(app, [plan], backup_root)
            manifest = patcher.load_manifest(backup)
            manifest["state"] = "committed"
            patcher.write_json_atomic(backup / "manifest.json", manifest)
            bundle.write_text("new Cursor update", encoding="utf-8")
            args = type(
                "Args",
                (),
                {
                    "app": app,
                    "backup": backup,
                    "backup_root": backup_root,
                    "dry_run": False,
                },
            )()
            with self.assertRaisesRegex(patcher.PatcherError, "Refusing to overwrite"):
                patcher.command_restore(args)
            self.assertEqual(bundle.read_text(encoding="utf-8"), "new Cursor update")


if __name__ == "__main__":
    unittest.main()
