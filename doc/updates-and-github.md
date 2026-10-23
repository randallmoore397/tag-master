# Automatic updates and GitHub releases

BluePeak TAG Master updates itself. There is no update feed field, no "Check now"
button and no "Install update" button in the app. Every installation checks the
GitHub release feed, downloads a newer installer, verifies it and installs it
without asking the user.

## How it works

| Part | Value |
| --- | --- |
| Current version | `APP_VERSION` in `bluepeak_tag_master.py` |
| Update feed | `UPDATE_FEED_URL` in `bluepeak_tag_master.py` |
| Default feed | `https://github.com/randallmoore397/tag-master/releases/latest/download/update-manifest.json` |
| GitHub repository | `randallmoore397/tag-master` (public, branch `main`) |
| Check interval | Twice a day (every 12 hours), plus once shortly after startup |
| Download limit | 250 MB |
| Required package | Windows `.exe` installer |
| Integrity check | SHA-256 from the release manifest |

The update thread does this:

1. Waits for the startup SQLite Cloud pull to finish, so a pull cannot overwrite
   the result of a check.
2. Reads the manifest from `UPDATE_FEED_URL`.
3. Compares the manifest `version` with `APP_VERSION`.
4. If it is newer, downloads the installer to the application folder.
5. Rejects the update unless the SHA-256 matches the manifest and the file is a
   `.exe`.
6. Starts the installer silently and shuts the app down. The installer waits for
   the app to exit, keeps `*.db`, `*.db-wal`, `*.db-shm`, logs and backups, then
   starts the new version.

If a check or download fails, the error is written to
`bluepeak_tag_master.log` and `update_error` in the local database. The app
clears `update_last_check` so it retries on the next loop instead of waiting a
full day.

Update runtime values (`update_last_check`, `update_latest`, `update_notes`,
`update_error`) are local to each computer and are not pushed to or pulled from
SQLite Cloud. This prevents one computer's check time from suppressing checks on
another computer.

## Release manifest

The build script writes `Output/update-manifest.json`:

```json
{
  "version": "1.5.0",
  "url": "https://github.com/randallmoore397/tag-master/releases/latest/download/BluePeak-TAG-Master-Setup.exe",
  "sha256": "64 lowercase hex characters",
  "notes": "Short release notes"
}
```

The file is written as UTF-8 without a BOM. The app reads it with `utf-8-sig` so
an older file with a BOM still works.

## Publishing a new version

### 1. Update the code and version

Make the changes, then bump `APP_VERSION` in `bluepeak_tag_master.py` using
semantic versioning (`1.4.1` -> `1.5.0` for features, `1.4.1` -> `1.4.2` for a
fix).

### 2. Run the tests

```powershell
python -m unittest discover -s tests
```

All tests must pass before publishing.

### 3. Commit and push

```powershell
git add -A
git commit -m "Release 1.5.0"
git push
```

### 4. Build the installer and manifest

Run from the project folder:

```powershell
.\build-release.ps1
```

The script reads `APP_VERSION`, builds `dist-release`, runs Inno Setup, and writes:

- `Output\BluePeak-TAG-Master-Setup.exe`
- `Output\update-manifest.json`

The default `-BaseUrl` points at the GitHub release download folder. Use
`-Notes "..."` to put release notes in the manifest.

Requirements: Python 3 with PyInstaller, and Inno Setup 6 installed at the
default location or passed with `-Iscc`.

### 5. Publish the GitHub release

Create a fine-grained GitHub token for the repository with **Contents: Read and
write**. Pass it only for this command:

```powershell
$env:GITHUB_TOKEN = "<token>"
.\publish-github.ps1
```

The script creates or replaces the release whose tag is `v<APP_VERSION>`
(for example `v1.5.0`) and uploads both assets. GitHub then serves them at the
stable `releases/latest/download` URLs, which is exactly what the app uses.

### 6. Verify

Check that the public manifest is reachable and contains the new version:

```powershell
Invoke-RestMethod "https://github.com/randallmoore397/tag-master/releases/latest/download/update-manifest.json"
```

You can also run the app's own parser:

```powershell
python -c "import bluepeak_tag_master as a; print(a._fetch_update_manifest(a.UPDATE_FEED_URL))"
```

To make an already-running copy check immediately for testing, clear
`update_last_check` in its local database, then restart the app. With a newer
release published, the app downloads and installs it automatically.

## GitHub repository setup

1. Create a **public** repository, for example `tag-master`.
2. Push this project to `main`.
3. The feed uses the public, unauthenticated GitHub release URL, so the app
   cannot read a private repository. Keep the repository public.
4. Create a fine-grained personal access token limited to this repository with
   **Contents: Read and write**. Do not commit the token and do not put it in the
   app.
5. Revoke the token when it is no longer needed. If a token is ever pasted into
   a chat or a file, revoke it immediately and create a new one.

The app does not store a GitHub token. It only reads the public manifest and
installer.

## Files involved

| File | Purpose |
| --- | --- |
| `bluepeak_tag_master.py` | `UPDATE_FEED_URL`, `APP_VERSION`, update check, download, install, updater UI |
| `build-release.ps1` | Builds the app, installer and BOM-free manifest |
| `publish-github.ps1` | Creates the GitHub release and uploads the two assets |
| `BluePeak-TAG-Master.iss` | Inno Setup script; protects the database and backups |
| `BluePeak-TAG-Master.spec` | PyInstaller onedir build |
| `Output/` | Latest installer and manifest |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No update is found | Is the release newer than `APP_VERSION`? Is `v<version>` the latest GitHub release? |
| HTTP 404 | The release must contain `update-manifest.json` and `BluePeak-TAG-Master-Setup.exe`. |
| "The update file is not valid JSON" | The manifest was probably written with a BOM or is not JSON. Rebuild with `build-release.ps1`. |
| "did not match its checksum" | The manifest and installer are from different builds. Rebuild, then publish both again. |
| Installer never finishes | Check `bluepeak_tag_master.log`; make sure the installer is not waiting for another copy of the app. |
| Cloud errors in the log | Errors such as `no healthy upstream` come from SQLite Cloud, not from the updater. Updates work without cloud sync. |

Log file: `%LOCALAPPDATA%\BluePeak TAG Master\bluepeak_tag_master.log`

## Changing the repository

If the project moves to another GitHub account or repository, update all three
places together:

1. `UPDATE_FEED_URL` in `bluepeak_tag_master.py`.
2. The default `-BaseUrl` in `build-release.ps1`.
3. The default `-Repo` in `publish-github.ps1`.

Then build and publish a new release so installed copies receive the new feed
through the version they already have. Existing installations keep using the old
feed until they install that release.
