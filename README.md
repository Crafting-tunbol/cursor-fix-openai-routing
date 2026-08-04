# Cursor Custom API Routing Fix

A guarded macOS patcher for a Cursor routing bug where an enabled OpenAI API key
is attached to explicitly selected Cursor-hosted models such as Composer or
Cursor Grok.

The patch changes routing, not the error message:

- OpenAI BYOK is used only for OpenAI-family model IDs or user-added models.
- Cursor-hosted models do not inherit the OpenAI key.
- The custom OpenAI base URL is attached only when an OpenAI key is attached.
- Claude, Gemini, Azure, and Bedrock routing is left unchanged.

## Requirements

- macOS
- Python 3.10 or newer
- An installed `Cursor.app`

The script uses only Python's standard library and macOS tools (`ditto` and
`codesign`).

## Check compatibility

```bash
python3 cursor_fix_custom_api.py status --app "/Applications/Cursor.app"
```

The patcher recognizes the vulnerable code by structure rather than hard-coded
minified symbols. It supports symbol-name changes between builds. It
deliberately stops if a future Cursor version changes the relevant logic or
produces ambiguous matches; no script can safely guarantee compatibility with
every unknown future version.

## Patch a clone (recommended)

```bash
python3 cursor_fix_custom_api.py patch \
  --app "/Applications/Cursor.app" \
  --output "$HOME/Applications/Cursor Custom API Fix.app"
```

Clone mode is non-destructive: it leaves `/Applications/Cursor.app` untouched.
The patcher modifies only files inside the cloned app bundle, while backup
copies and manifests are written to the backup directory documented below.

The clone intentionally shares Cursor's existing global configuration. It does
not modify or interfere with your settings, extensions, conversations, or API
keys, and it does not create a separate profile for them.

Add `--trace` to emit safe routing diagnostics. Traces contain model names and
boolean attachment flags, never API keys or base URL values.

Preview without writing:

```bash
python3 cursor_fix_custom_api.py patch --dry-run --trace
```

If the destination already exists and should be recreated:

```bash
python3 cursor_fix_custom_api.py patch --force-output
```

## After installation

1. Open the new fixed app.
2. macOS or Cursor will warn that the installation files were modified. This
   warning is expected for a locally fixed application.
3. When asked to access `Cursor Safe Storage`, authorize it and choose
   **Always Allow**.
4. The first startup may show “Unable to resolve your shell environment in a
   reasonable time” if the Safe Storage prompt blocked startup.
5. After granting access, fully quit the fixed app and open it again. The shell
   environment warning should then be resolved.

## Backups

Before changing a bundle, the script creates a timestamped backup under:

```text
~/Library/Application Support/Cursor Custom API Fix/backups/
```

Each backup contains:

- The exact original bytes of every modified bundle
- A JSON manifest
- Cursor version and target application path
- Original and patched SHA-256 checksums

No backup is created during `--dry-run` or when the app is already patched.

## Restore

Restore the newest backup associated with the patched app:

```bash
python3 cursor_fix_custom_api.py restore \
  --app "$HOME/Applications/Cursor Custom API Fix.app"
```

Restore a specific backup:

```bash
python3 cursor_fix_custom_api.py restore \
  --app "$HOME/Applications/Cursor Custom API Fix.app" \
  --backup "$HOME/Library/Application Support/Cursor Custom API Fix/backups/cursor-VERSION-TIMESTAMP"
```

Preview a restore:

```bash
python3 cursor_fix_custom_api.py restore \
  --app "$HOME/Applications/Cursor Custom API Fix.app" \
  --dry-run
```

Restoration validates backup checksums, atomically restores each file, and
verifies the application. It refuses to overwrite a file whose hash differs
from both the recorded original and patched versions, preventing an old backup
from silently replacing files installed by a later Cursor update.

Restored bundle payloads are byte-for-byte identical to their recorded
originals. Clone mode preserves the untouched source application.

## In-place patching

Patching the original installation is supported but discouraged:

```bash
python3 cursor_fix_custom_api.py patch \
  --app "/Applications/Cursor.app" \
  --in-place
```

Writing under `/Applications` may require permissions. Cursor updates can
overwrite an in-place patch.

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
