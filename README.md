# Cursor OpenAI Routing Fix

Because Cursor won't fix it.

A guarded Python patcher for the internal JS routing bug that causes
"This model does not support custom APIs" when an enabled OpenAI API key is
incorrectly attached to Cursor-hosted models such as Composer or Cursor Grok.

Supports **macOS** and **Windows** (including patching a Windows install from
WSL by pointing `--app` at the Windows path).

The patch changes routing, not the error message:

- OpenAI BYOK is used only for OpenAI-family model IDs or user-added models.
- Cursor-hosted models do not inherit the OpenAI key.
- The custom OpenAI base URL is attached only when an OpenAI key is attached.

## Requirements

- macOS, Windows, or WSL
- Python 3.10 or newer
- An installed Cursor application

The script uses only Python's standard library. On macOS it also uses
`ditto` and `codesign`. On Windows there is no codesign step.

## Check compatibility

macOS:

```bash
python3 cursor_fix_openai_routing.py status --app "/Applications/Cursor.app"
```

Windows (PowerShell or cmd):

```bash
python cursor_fix_openai_routing.py status --app "$env:LOCALAPPDATA\Programs\cursor"
```

WSL against a Windows install:

```bash
python3 cursor_fix_openai_routing.py status \
  --app "/mnt/c/Program Files/cursor"
# or, for a per-user install:
python3 cursor_fix_openai_routing.py status \
  --app "/mnt/c/Users/$USER/AppData/Local/Programs/cursor"
```

The patcher recognizes the vulnerable code by structure rather than hard-coded
minified symbols. It supports symbol-name changes between builds. It
deliberately stops if a future Cursor version changes the relevant logic or
produces ambiguous matches; no script can safely guarantee compatibility with
every unknown future version.

## Patch a clone (recommended)

### macOS

```bash
python3 cursor_fix_openai_routing.py patch \
  --app "/Applications/Cursor.app" \
  --output "$HOME/Applications/Cursor OpenAI Routing Fix.app"
```

### Windows

```bash
python cursor_fix_openai_routing.py patch `
  --app "$env:LOCALAPPDATA\Programs\cursor" `
  --output "$env:LOCALAPPDATA\Programs\Cursor OpenAI Routing Fix"
```

Prefer a per-user install under `%LOCALAPPDATA%\Programs\cursor`. System-wide
installs under `C:\Program Files\cursor` often require elevation to modify.

Clone mode is non-destructive: it leaves the original Cursor install untouched.
The patcher modifies only files inside the clone, while backup copies and
manifests are written to the backup directory documented below.

The clone intentionally shares Cursor's existing global configuration. It does
not modify or interfere with your settings, extensions, conversations, or API
keys, and it does not create a separate profile for them.

Add `--trace` to emit safe routing diagnostics. Traces contain model names and
boolean attachment flags, never API keys or base URL values.

Preview without writing:

```bash
python3 cursor_fix_openai_routing.py patch --dry-run --trace
```

If the destination already exists and should be recreated:

```bash
python3 cursor_fix_openai_routing.py patch --force-output
```

## After installation

### macOS

1. Open the new fixed app.
2. macOS or Cursor will warn that the installation files were modified. This
   warning is expected for a locally fixed application.
3. When asked to access `Cursor Safe Storage`, authorize it and choose
   **Always Allow**.
4. The first startup may show “Unable to resolve your shell environment in a
   reasonable time” if the Safe Storage prompt blocked startup.
5. After granting access, fully quit the fixed app and open it again. The shell
   environment warning should then be resolved.
6. Cursor may show **“Your Cursor installation appears to be corrupt. Please
   reinstall”** or mark the window as Unsupported. That comes from Cursor's
   cross-platform integrity check of patched files—not a macOS-only check. It
   is safe to ignore; do not reinstall solely because of it. Some machines never
   show the banner (for example if it was dismissed for this build, or if a
   pending update suppresses it).
7. Optional: replace your usual Dock / Applications shortcut with the fixed app
   path so everyday launches use the patched build.

### Windows

1. Open the fixed `Cursor.exe`.
2. Cursor may warn that the installation files were modified. This is expected.
3. Cursor may show **“Your Cursor installation appears to be corrupt. Please
   reinstall”** or mark the window as Unsupported. Same integrity check as on
   macOS; safe to ignore if it appears. Some Windows installs never show it.
4. Optional: replace your Start Menu / taskbar / desktop Cursor shortcut so it
   points at the fixed install path for convenience.

## Backups

Before changing a bundle, the script creates a timestamped backup under:

macOS:

```text
~/Library/Application Support/Cursor OpenAI Routing Fix/backups/
```

Windows / WSL (Windows profile):

```text
%LOCALAPPDATA%\Cursor OpenAI Routing Fix\backups\
```

Each backup contains:

- The exact original bytes of every modified bundle
- A JSON manifest
- Cursor version, layout (`macos` / `windows`), and target application path
- Original and patched SHA-256 checksums

No backup is created during `--dry-run` or when the app is already patched.

## Restore

Restore the newest backup associated with the patched app:

macOS:

```bash
python3 cursor_fix_openai_routing.py restore \
  --app "$HOME/Applications/Cursor OpenAI Routing Fix.app"
```

Windows:

```bash
python cursor_fix_openai_routing.py restore `
  --app "$env:LOCALAPPDATA\Programs\Cursor OpenAI Routing Fix"
```

Restore a specific backup:

```bash
python3 cursor_fix_openai_routing.py restore \
  --app "$HOME/Applications/Cursor OpenAI Routing Fix.app" \
  --backup "$HOME/Library/Application Support/Cursor OpenAI Routing Fix/backups/cursor-VERSION-TIMESTAMP"
```

Preview a restore:

```bash
python3 cursor_fix_openai_routing.py restore \
  --app "$HOME/Applications/Cursor OpenAI Routing Fix.app" \
  --dry-run
```

Restoration validates backup checksums, atomically restores each file, and on
macOS re-signs the application. It refuses to overwrite a file whose hash
differs from both the recorded original and patched versions, preventing an
old backup from silently replacing files installed by a later Cursor update.

Restored bundle payloads are byte-for-byte identical to their recorded
originals. Clone mode preserves the untouched source application.

## In-place patching

Patching the original installation is supported but discouraged:

macOS:

```bash
python3 cursor_fix_openai_routing.py patch \
  --app "/Applications/Cursor.app" \
  --in-place
```

Windows:

```bash
python cursor_fix_openai_routing.py patch `
  --app "$env:LOCALAPPDATA\Programs\cursor" `
  --in-place
```

Writing under `/Applications` or `C:\Program Files` may require elevated
permissions. Cursor updates can overwrite an in-place patch.

## Updating Cursor

After an update:

1. Run `status` against the new original app.
2. Run `patch --force-output` to recreate the clone.
3. If the script reports an unsupported or ambiguous bundle, do not force a
   textual replacement. The routing structure changed and the patcher needs an
   update.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
