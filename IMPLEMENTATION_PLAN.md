# BluePeak TAG Master — Implementation Plan

Date: 2026-10-19
Companion document: `CODE_REVIEW_FINDINGS.md` (item numbers below match that document).
Goal: harden the app and land the highest-value improvements in small, individually shippable, verifiable steps — without breaking the single-file/stdlib-only design until the structural phase is explicitly started.

## Guiding principles

- **Ship small.** Each phase ends with a working build (`pyinstaller` + Inno Setup) and a manual smoke test.
- **No new runtime dependencies.** The app is stdlib-only by design; keep it that way unless a phase explicitly opts in.
- **Backwards compatible data.** Any schema change must be additive and idempotent in `init_db`, guarded by `PRAGMA user_version`.
- **Test what we fix.** Introduce a minimal test harness in Phase 0 so each fix gets a regression test.
- **Keep `CODE_REVIEW_FINDINGS.md` item IDs** in commit messages (e.g. `fix(A1):`).

## Current build/test commands

- Build: `python -m PyInstaller --clean --noconfirm "BluePeak-TAG-Master.spec"`
- Installer: `& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" "BluePeak-TAG-Master.iss"`
- Compile check: `python -c "import py_compile; py_compile.compile('bluepeak_tag_master.py', doraise=True)"`
- Manual smoke test: launch exe with `--no-browser --port <p>`, GET `/logo.png` and `/`, confirm DB is created.

---

## Phase 0 — Test harness + schema versioning

**Why:** Everything else needs a way to verify; schema changes need a version gate. (E45)

1. Add `tests/` with a plain `unittest` suite (stdlib) that imports `bluepeak_tag_master` against a temp DB, mirroring the ad-hoc check already used to validate the cloud save fix.
2. Add `PRAGMA user_version` handling in `init_db` (`TAG:243-253`): read current version, apply ordered additive migrations, bump after success. Start at version 1 (current schema).
3. Add a `run_tests.ps1` (or document `python -m unittest discover -s tests`).

**Exit criteria:** `python -m unittest discover -s tests` passes; `init_db` is idempotent on an existing DB and records `user_version`.

---

## Phase 1 — Fix-now bugs and security (small, high value)

Each item is independent and shippable.

1. **A1 — Bulk action IndexError.** In `api_devices_bulk` (`TAG:1041`), after fetching rows by id add `if not rows: raise ApiError(400, "Those devices no longer exist.")`. Add a test proving a stale-id request returns 400, not 500.
2. **B11 — 500 handler leakage.** At the 500 handler (`TAG:3140-3142`) log the full exception via `write_log` and return a generic message (`"Something went wrong. Check the log for details."`). Keep the client-safe `ApiError` path untouched.
3. **A3 — Cloud push fingerprint race.** Take the fingerprint from the *pushed snapshot* rather than the live DB. Concretely: have `bluepeak_cloud.push()` return the fingerprint it read (`CLOUD:185-238`), and in `cloud_run` (`TAG:2764-2767`) persist that value instead of a fresh `fingerprint(DB_PATH)`.
4. **B20 — Viewer export gating.** Decide policy (recommended: exports require `write`). Change `ROUTES` perms for `/api/export.csv`, `/api/export.xlsx`, `/api/labels/log` (`TAG:2942`, `TAG:3079`) and note the decision in a short `SECURITY.md` or a code comment.
5. **B15 — Mask `token=` too.** Extend `mask_url` (`TAG:2686`) to redact both `apikey=` and `token=`. Add a unit test with both forms.
6. **A6 — Stale-row print guard.** In `openLabels` (`TAG:3897`) filter out `undefined` rows and show a toast if none remain; guard the call site (`TAG:4175`).
7. **A8 — Restore label string.** Fix the format string at `TAG:2457-2459` to produce clean audit text without the `.replace("  ", " ")` hack.

**Exit criteria:** each fix has a test or a documented manual repro; full suite green; exe + installer rebuilt and smoke-tested.

---

## Phase 2 — Sync correctness, audit paging, and backup safety

1. **A2 — Audit pagination OFFSET.** Add `offset` to `/api/audit` (`TAG:2576-2588`) using `LIMIT ? OFFSET ?`; update the client "Show more" (`TAG:4369-4371`) to append instead of re-render, preserving expanded detail state (`TAG:30`).
2. **F59 — Scheduled/rotating local backup.** Add a nightly backup (reuse `_backup_local`, `TAG:2734-2746`) triggered by a lightweight timer in the server loop, keeping the existing "keep last 5" retention. Expose last-backup time in the Settings "Your data" panel.
3. **A5 — Import bounds + batching.** Cap import rows per request and process in batched transactions in `api_import` (`TAG:1119-1146`); return clear per-batch results.
4. **D41 + A3 follow-up — Fingerprint robustness.** Strengthen the large-table fingerprint (`CLOUD:395-408`) so delete+insert and same-second `updated_at` collisions are detected (e.g. also hash `min`/`max` rowid and a checksum of a sampled column). Add tests.

**Exit criteria:** audit log loads incrementally and preserves state; a backup appears nightly and survives restart; importing 10k rows doesn't lock the app; sync detects a delete+insert.

---

## Phase 3 — Performance

1. **D36 — Serial lookup.** Add a normalized `serial_lower` column (or `COLLATE NOCASE`) and index it so the duplicate check (`TAG:740-743`) stops scanning. Backfill in a migration (Phase 0 gate).
2. **D35 — Search.** Evaluate FTS5 virtual table mirroring the 10 searchable columns; keep `LIKE` as fallback. Benchmark before/after with a seeded 10k-device DB.
3. **D37 — Audit facets.** Cache the DISTINCT facets (`TAG:2593-2597`) or maintain them incrementally; replace the per-row correlated `COUNT(*)` (`TAG:2587`) with a grouped pre-query.
4. **D39 — Sort/filter indexes.** Add indexes for the `COLLATE NOCASE` sort/filter columns (`TAG:666-671`, `TAG:695`) where profiling shows a full sort.

**Exit criteria:** measured before/after on a seeded DB; document the numbers.

---

## Phase 4 — UX polish

1. **C23 — Self-host or drop Google Fonts** (`TAG:3275`) so the app is fully offline.
2. **C25 — Responsive inventory table** (`TAG:4156`): column-priority or card view at narrow widths.
3. **C26/C30 — Loading states and incremental "Show more"** for the table and Activity feed.
4. **C28 — CSV import dry-run/preview** with a downloadable rejected-rows report before commit.
5. **C34 — Restore-from-backup flow** in Settings (stop app → swap DB → restart), with confirmation.
6. **C27 — Persistent/expandable error surface** instead of 3.4 s toasts-only for failures.

**Exit criteria:** app usable on a phone-width viewport; import never commits silently-failed rows; restore is a guided flow.

---

## Phase 5 — Missing features (product)

Prioritize by user value; each is its own mini-project with schema changes gated by Phase 0.

1. **F52 — Warranty/EOL dates** (do early: schema change; add `warranty_end`, `expected_life`; "expiring soon" report).
2. **F61 — CSV import update/merge mode**.
3. **F54 — Location entity + bulk transfer** with from→to history.
4. **F53 — Scan-driven workflow** (scan-to-open, quick check-in/out; QR could encode a deep-link URL to the device record).
5. **F57 — API tokens + docs** for integration.
6. **F51 — Photos/attachments** (largest; needs file storage + a files table).
7. **F60 — Cloud conflict view** (diff local vs cloud using the fingerprint machinery).
8. **F55/F56/F58/F62 — parent/child links, granular roles, audit retention, diagnostics page.**

**Exit criteria per feature:** schema migration, API, UI, tests, and a Settings/dashboard touchpoint.

---

## Phase 6 — Structural refactor (largest, do last)

1. **E43 — Split the monolith** into modules along existing seams: `bluepeak_tags.py`, `bluepeak_pdf.py`, `bluepeak_qr.py`, `bluepeak_xlsx.py`, `bluepeak_audit.py`, plus a static `index.html` served from disk (bundled via the `.spec`).
2. **E44 — Single-source QR/Code-128 tables** (Python ↔ JS) with shared cross-language test vectors so on-screen labels and printed PDFs can't drift.
3. **E46 — Unify version strings**; **E47 — de-duplicate helpers**; **E48 — remove per-request class attrs**; **E49 — structured logging + rotation**; **E50 — add README/requirements.**
4. Update the PyInstaller `.spec` to bundle the static assets after the split.

**Exit criteria:** behavior identical (golden-file tests for PDF/XLSX/QR output), bundle still builds and runs, tests green.

---

## Rollout / risk

- After **every** phase: rebuild exe + installer, run the smoke test, and verify SQLite Cloud push/pull still works end-to-end.
- Schema changes ship with a pre-migration backup (reuse `_backup_local`).
- Keep the shipped installer's "preserve DB on uninstall" behavior.
- Phase ordering is deliberate: correctness/security first, then data-safety, then perf, then UX, then features, then the refactor that touches everything.

## Suggested commit discipline

`fix(A1): guard bulk action against stale ids`
`fix(B11): stop leaking exception text in 500 responses`
`feat(E45): schema user_version + additive migrations`
`perf(D36): index normalized serial column`
`refactor(E43): extract bluepeak_pdf.py`
