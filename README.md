# BluePeak TAG Master

BluePeak TAG Master is a Windows desktop application for tagging, tracking and
auditing equipment. It runs as a local web app, stores everything in a SQLite
database on the computer, and can mirror the data to SQLite Cloud.

## Features

- Device inventory with tags, serial numbers, categories, locations, status,
  warranty dates, notes and full change history
- Barcode and QR label printing, on-screen labels, PDF sheets and single labels
- CSV and Excel export, database backups and restore
- Users and roles (administrator, technician, viewer)
- Audit trail and per-device history snapshots
- Optional SQLite Cloud sync with automatic push and startup pull
- Automatic, mandatory updates delivered through GitHub Releases
- Light and dark themes, keyboard search and tag scanning

## Requirements

For the installed application:

- Windows 10 or later

For development and building:

- Python 3.10 or later (standard library only at runtime)
- PyInstaller for building the executable
- Inno Setup 6 for building the installer
- PowerShell 5.1 or later

## First run

Double-click `BluePeak TAG Master.exe` after installation, or run
`Start-BluePeak-TAG-Master.bat` from the project folder.

The first sign-in is:

- Username: `admin`
- Password: `BluePeak@2026`

The app asks for a new password immediately. Change it before entering real
data.

The local database is stored next to the executable as
`bluepeak_tag_master.db`. The Windows installer never overwrites the database,
its WAL files, logs or the `backups` folder.

## Automatic updates

Updates are not optional and there is nothing to configure in the app. Every
copy checks the GitHub release feed shortly after startup and every 12 hours.
When a newer version exists, it downloads the installer, verifies its SHA-256
checksum and installs it silently. The app closes, the installer keeps the
database and user data, and the new version starts again.

See [doc/updates-and-github.md](doc/updates-and-github.md) for the release
workflow, GitHub setup, manifest format and troubleshooting.

## Run from source

```powershell
python bluepeak_tag_master.py
```

Useful options:

```powershell
python bluepeak_tag_master.py --no-browser
python bluepeak_tag_master.py --port 8899
```

The app opens at `http://127.0.0.1:8765` by default.

## Tests

```powershell
python -m unittest discover -s tests
```

The test suite covers inventory, tag generation, labels, users, audit history,
imports, backups, cloud sync ordering and update checks.

## Build a release

```powershell
.\build-release.ps1
```

Output:

- `Output\BluePeak-TAG-Master-Setup.exe`
- `Output\update-manifest.json`

Publish to GitHub Releases:

```powershell
$env:GITHUB_TOKEN = "<fine-grained token with Contents: Read and write>"
.\publish-github.ps1
```

The publication script creates the `v<version>` release and uploads both assets.
Bump `APP_VERSION` in `bluepeak_tag_master.py` before building a release.

## Project layout

| Path | What it contains |
| --- | --- |
| `bluepeak_tag_master.py` | Application, HTTP server, database, UI, updater |
| `bluepeak_cloud.py` | SQLite Cloud push, pull and fingerprints |
| `tests/test_bluepeak.py` | Automated tests |
| `BluePeak-TAG-Master.iss` | Inno Setup installer definition |
| `BluePeak-TAG-Master.spec` | PyInstaller build definition |
| `build-release.ps1` | Builds the executable, installer and manifest |
| `publish-github.ps1` | Publishes the GitHub release |
| `doc/` | Additional documentation |

## Data and safety

- Back up the local database from **Settings > Your data** before large changes
  or before moving to another computer.
- The application keeps working without SQLite Cloud. Cloud sync is a mirror,
  not a replacement for local data.
- Update packages must be `.exe` files served over HTTPS and must match the
  SHA-256 in the manifest. Unsigned or altered packages are rejected.
- Never commit the database, logs, backups or GitHub tokens to the repository.
