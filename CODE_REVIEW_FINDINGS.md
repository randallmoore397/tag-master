# BluePeak TAG Master — Code Review Findings

Date: 2026-10-19
Scope: `bluepeak_tag_master.py` (~4,690 lines, single-file stdlib HTTP server + embedded HTML/CSS/JS SPA) and `bluepeak_cloud.py` (~440 lines, SQLite Cloud Weblite REST client).
Method: full read of both source files; every item below cites `file:line`.

Overall: the app is genuinely solid — stdlib-only, scrypt/PBKDF2 password hashing with per-user salt and `hmac.compare_digest`, session tokens stored only as SHA-256 hashes, `HttpOnly` + `SameSite=Strict` cookies, server bound to `127.0.0.1`, security headers on every response, and a full audit log. Most findings are polish and hardening rather than rewrites.

`TAG` = `bluepeak_tag_master.py`; `CLOUD` = `bluepeak_cloud.py`. Severity: high / med / low. Effort: small / med / large.

---

## A) Correctness / bugs

1. **Bulk action crashes with IndexError (→ 500).** `api_devices_bulk` computes `span = rows[0]["tag"]...` after fetching rows by id, but if all supplied ids are stale (devices deleted since selection), `rows` is empty and `rows[0]`/`rows[-1]` raise IndexError. There is an `if not ids` guard (TAG:1033) but not a `if not rows` guard. — **high / small** (add `if not rows: raise ApiError(400, ...)`). `TAG:1041`
2. **Audit pagination has no OFFSET.** The server query is `... ORDER BY a.id ? LIMIT ?` only; the client's "Show more" button does `A.limit += 200; load()`, so each click re-downloads every previously seen row. For a large audit log this grows quadratically and re-renders the whole `<ul>`, losing expanded details. — **med / small** (accept `offset`, use `LIMIT ? OFFSET ?`). `TAG:2576-2588`, `TAG:4369-4371`
3. **Push can mark un-pushed edits as synced.** `bluepeak_cloud.push()` reads tables under a deferred `BEGIN` while the upload (many HTTP round-trips at `BATCH=120`) runs; local writes committed during the upload aren't in the pushed snapshot. Afterward `cloud_run` sets `cloud_fp` to a *fresh* fingerprint of the DB, so `cloud_out.pending` becomes false and auto-push skips those edits until the next local change. — **high / med** (take the fingerprint before pushing, inside the same snapshot). `TAG:2762-2767`, `CLOUD:185-238`
4. **Preview "last tag" can be wrong.** `api_preview` computes `last = build_tag(..., seq + qty - 1)` arithmetically, but `allocate()` skips tags that already exist, so with holes the real last generated tag can differ from the previewed one. — **low / small**. `TAG:826-829`, `TAG:646-651`
5. **Import accepts unbounded row counts in one transaction.** `api_import` loops over every submitted row inside a single `tx`; only the 200 MB body cap limits it, and the client sends whatever the CSV contains. A very large CSV means one enormous `BEGIN IMMEDIATE` blocking all other writers. — **med / small** (cap rows, batch transactions). `TAG:1119-1146`, `TAG:4302-4308`
6. **Print action can TypeError on stale rows.** `openLabels([I.rows.find(r=>r.id===id)])` passes `[undefined]` if the row lookup fails; `openLabels` only checks `devs.length` then `x.tag` crashes. — **low / small**. `TAG:4175`, `TAG:3897`
7. **Bulk edit bypasses field normalisation.** Bulk `fields` are truncated and status-checked, but never run through `clean_fields`, so MAC normalisation and the assigned_to↔status coupling applied on single edit differ between the two paths. — **low / med**. `TAG:1012-1030`, `TAG:952-957`
8. **Malformed restore label string.** The format `"%s %s the '%s' by %s on %s UTC"` is passed an empty string as the second argument and then patched with `.replace("  ", " ")` — works but hides a formatting bug. — **low / small**. `TAG:2457-2459`
9. **`next_seq`/`allocate` probe one SELECT per candidate tag.** Generating a batch after a large gap between the counter and existing tags issues one `SELECT 1 FROM devices WHERE tag=?` per skipped value, inside the write transaction. — **low / med**. `TAG:631-637`, `TAG:646-651`
10. **Audit "years" facet recomputes over the whole table on every request.** Four DISTINCT scans (users/actions/categories/years) run per `/api/audit` call even when the client already cached the dropdown. — **low / med**. `TAG:2596-2597`

## B) Security

11. **500 handler leaks exception text to the client.** `send(500, {"error": "Something went wrong: %s" % e})` can expose SQL fragments, file paths, or cloud URLs. Log the detail, return a generic message. — **high / small**. `TAG:3140-3142`
12. **Session cookie has no `Secure` flag.** `cookie_value` sets `Path=/; HttpOnly; SameSite=Strict; Max-Age` but not `Secure`. Acceptable only because the server is plain-HTTP-on-localhost; any TLS/reverse-proxy deployment silently loses protection. — **med / small**. `TAG:395-396`
13. **Hard-coded default admin password printed to stdout.** `DEFAULT_ADMIN = ("admin", "BluePeak@2026")` is baked into the binary and printed on first run; `must_change` mitigates but the credential exists until first sign-in. — **med / small** (generate a random first-run password). `TAG:325`, `TAG:3230-3233`
14. **Password hashes and session token hashes are synced to SQLite Cloud.** `SYNC_TABLES` includes `users` and `sessions`, so `password_hash` and `token_hash` values are pushed to the remote database in plaintext SQL. Only `settings` rows with the `cloud_` prefix are filtered. — **med / med**. `CLOUD:28`, `CLOUD:203-205`, `CLOUD:226`
15. **`mask_url` only masks `apikey=`, not `token=`.** The connection string may carry the key as `?token=...`, so a token-bearing URL is shown unmasked in Settings, printed at startup, and can land in the log via `_sync_error`. — **med / small**. `TAG:2686`, `CLOUD:51`, `TAG:2705`, `TAG:3228`, `TAG:2725`
16. **API key stored in plaintext in the settings table** (and therefore in every downloaded backup). Fine for a local tool, but no OS-keychain/DPAPI option. — **low / med**. `TAG:2712-2720`, `TAG:3111`
17. **`_pull_sql` builds INSERTs from cloud-returned column names.** `cols` come from remote JSON keys and are interpolated as `"%s"`; a `"` in a remote column name breaks out of the quoting. Low likelihood, but the trust boundary is remote JSON. — **low / small**. `CLOUD:345-352`
18. **Manual SQL literal escaping (`_lit`) instead of parameter binding.** Doubling quotes and stripping NULs is the only thing between synced data and SQL injection into the cloud database. — **med / med**. `CLOUD:163-175`
19. **Failed-login lockout is in-memory only.** `_fail` is lost on restart, per-process, and keyed by username, not source; an attacker can hammer different usernames without limit. — **low / med**. `TAG:327-328`, `TAG:458-470`
20. **Viewer role can export the whole inventory.** `/api/export.csv`, `/api/export.xlsx`, `/api/labels/log` are perm `"any"`, so a read-only viewer can exfiltrate the full device list (notes, assignees, serials) with one click. — **med / small** (gate behind `write` or an explicit permission). `TAG:2942`, `TAG:3079`
21. **Host/Origin pinning exists but no real CSRF token.** `host_ok` + `X-Requested-With: BluePeak` + `SameSite=Strict` are present; there is still no CSRF token and no Origin check on GETs — acceptable for localhost, but document it. — **low / info**. `TAG:3043-3051`, `TAG:3075`
22. **Positive (for the record):** scrypt/PBKDF2 + per-user salt + `hmac.compare_digest`, session tokens stored only as SHA-256 hashes, `HttpOnly` + `SameSite=Strict`, server bound to `127.0.0.1`, login rate-limit with dummy hash for unknown users, security headers on every response. `TAG:336-364`, `TAG:409`, `TAG:483`, `TAG:3208`, `TAG:463`, `TAG:3031-3034`

## C) UX (embedded HTML/JS)

23. **Google Fonts loaded from the internet in a "no packages, local app" product.** Offline (the normal case) it stalls/falls back and reveals the app's network dependency. — **med / small** (self-host or drop). `TAG:3275`
24. **Global search box is hidden below 560 px**, so phone users lose the `/` shortcut search. — **low / small**. `TAG:3634`
25. **Inventory table has `min-width:1040px` with 10 columns and no responsive strategy.** On mobile it is pure horizontal scrolling through the densest part of the app. — **med / med**. `TAG:4156`
26. **No loading/busy state on table paging/filtering.** `loadRows()` re-renders only after the fetch resolves; old rows stay with no spinner, so slow responses look frozen. — **low / small**. `TAG:4201-4214`
27. **Error toasts auto-dismiss in 3.4 s like success toasts**, with no way to reopen them; several flows rely solely on toasts for failures. — **low / small**. `TAG:3739`, `TAG:4194`, `TAG:4246`, `TAG:4251`
28. **CSV import has no dry-run/preview and no per-row edit before commit.** Skipped-row errors appear only after the import has partially committed, and there's no "download the rejected rows" option. — **med / med**. `TAG:4292-4313`, `TAG:4310`
29. **Device history dialog silently truncates at 100 entries** with no "older changes" indicator or paging. — **low / small**. `TAG:906-911`, `TAG:3999`
30. **"Show more" in Activity re-renders the entire list**, collapsing expanded detail panes and jumping scroll position. — **low / med**. `TAG:4365-4371`
31. **Excel export offers no progress feedback beyond a toast.** For thousands of rows the server builds the whole workbook in memory with zero client-side indication. — **low / med**. `TAG:3122-3126`, `TAG:1917`
32. **Label-preview debounce races.** Out-of-order responses are not cancelled, so a stale preview can overwrite a newer one when changing category quickly. — **low / small**. `TAG:4013-4014`
33. **Roll-print page CSS is a string-built hack** with a 0.3 mm fudge factor to stop blank trailing pages; fragile across browsers/printers. — **low / small**. `TAG:3954`, `TAG:3957`
34. **No restore-from-backup flow.** A downloaded `.db` backup can only be restored by manually swapping the file while the app is stopped. — **med / med**. `TAG:4481`

## D) Performance

35. **Free-text search is `LIKE '%term%'` over 10 columns, AND-ed per word.** Every keystroke (debounced 220 ms) runs a full-table scan per token; no FTS5 or trigram index. — **med / med**. `TAG:672-686`, `TAG:4158`
36. **Duplicate-serial check wraps the column in `LOWER()`, defeating `idx_dev_serial`.** Every create/update/import row does a full scan of `devices`. — **med / small**. `TAG:740-743`, `TAG:211`
37. **Four DISTINCT full scans over `activity` per audit request**, plus a correlated `COUNT(*) ... WHERE parent_id=a.id` per returned row. The years facet uses `strftime` which no index can serve. — **med / med**. `TAG:2593-2597`, `TAG:2587`
38. **`size=0` fetches the entire table into memory.** Batch "Print labels"/"Copy tags" and "select all matching" materialise every matching row as JSON. — **low / med**. `TAG:834-836`, `TAG:4126`, `TAG:4139`
39. **Sorting by `brand`, `model`, `location`, `assigned`, `updated_at` uses `COLLATE NOCASE` expressions with no supporting indexes.** — **low / med**. `TAG:666-671`, `TAG:208-211`, `TAG:695`
40. **Backup serialises the whole DB to a temp file then reads it fully into memory**, twice (manual + per pre-pull backup); no streaming response. — **low / small**. `TAG:2983-2997`, `TAG:2734-2740`
41. **Fingerprint for tables >4000 rows is just `count:max(rowid)[:max(updated_at)]`.** Cheap, but a delete+insert pair or an `updated_at` collision within the same second is invisible; combined with bug A3 this can hide pending changes. — **med / small**. `CLOUD:395-408`
42. **Excel export loads every device row into a Python list** before building the workbook; the workbook is also fully in memory. No streaming/ceiling. — **low / med**. `TAG:1914-1918`, `TAG:2337-2341`

## E) Maintainability

43. **Single 4,690-line file** containing HTTP server, auth, domain logic, PDF generator, QR encoder, XLSX writer, and a ~1,400-line embedded HTML/CSS/JS app. Natural seams: `bluepeak_tags.py`, `bluepeak_pdf.py`, `bluepeak_qr.py`, `bluepeak_xlsx.py`, `bluepeak_audit.py`, plus a static `index.html`. — **high / large**. `TAG:3267` (embedded HTML start)
44. **Large algorithmic duplication between Python and JavaScript with no shared test vector.** QR encoder, Code-128, the 107-entry `C128` table, `UBASE`, `STATUSES`, and label font-size maths are duplicated; if one drifts, printed PDFs and on-screen labels disagree. — **high / med**. `TAG:1431-1581` vs `TAG:3790-3846`; `TAG:1175-1191` vs `TAG:3765-3772`; `TAG:1153`/`TAG:3764`; `TAG:1149`/`TAG:3876`; `TAG:57`/`TAG:3693`; `TAG:1264`/`TAG:3856`
45. **Zero tests and zero migrations.** No test files exist. Schema evolution is ad-hoc `PRAGMA table_info` + `ALTER TABLE` inside `init_db` with no `PRAGMA user_version`. — **high / med**. `TAG:243-253`
46. **Version strings disagree across the codebase** (`BluePeakTagMaster/1.1` vs cloud `BluePeak-TagMaster/1.2`). — **low / small**. `TAG:3004`, `CLOUD:79`
47. **Duplicated helper logic** (`_like()` duplicates inline escaping; `chunks()` exists but ids-from-body loops re-derive patterns; `csv_safe` applied inconsistently). — **low / small**. `TAG:2505-2506`, `TAG:684`, `TAG:2976-2977`
48. **`Handler` uses mutable class-level attributes `_cookie`/`user` reset per request** — works only because of careful reset ordering under the threading server. — **low / small**. `TAG:3022-3023`
49. **No structured logging or log rotation** — `write_log` appends unbounded to one file; the log receives raw cloud error strings which can contain connection details. — **low / small**. `TAG:89-96`, `TAG:2725`
50. **No dependency manifest or docs** — no README, no requirements.txt, and build artifacts are committed alongside sources. — **low / small**

## F) Missing features (device/asset-tracking expectations)

51. **No photos/attachments.** The `devices` table has only text fields; no upload endpoint, no file table, no camera/image control. — **med / large**. `TAG:191-207`
52. **No warranty / end-of-life dates.** Only `purchase_date` exists; no `warranty_end`, `expected_life`, or "warranty expiring soon" report. — **med / med**. `TAG:203`, `TAG:722-724`, `TAG:2156-2185`
53. **No scan-driven workflow.** QR/Code-128 data is generated but there's no camera/scan-to-open, no quick check-in/out by scan, and the QR encodes only the raw tag string (no URL). — **med / med**. `TAG:1175-1581`
54. **Location is free text, not a location entity, and there is no transfer workflow.** No bulk "move N devices from A to B" with from→to semantics, no transfer history view. — **med / med**. `TAG:202`, `TAG:1026-1028`
55. **No parent/child device links** (monitor↔PC, license↔device); `batch_id` only records generation time. — **low / med**. `TAG:205`
56. **Roles are a hardcoded 3-value enum** with no custom permissions, no per-category restrictions. — **med / med**. `TAG:324`, `TAG:433-436`
57. **No API documentation or API-token auth** — ~40 REST endpoints with no OpenAPI page, only browser session cookie + header, so scripting/integration isn't supported. — **med / med**. `TAG:2929-2965`
58. **No audit-log retention, archival, or pruning.** `activity` grows without bound; the only export is admin-only CSV. — **low / med**. `TAG:214-221`, `TAG:3082-3083`
59. **No scheduled backups** — only before a pull and on manual download; no nightly/rotating local backup. — **med / small**. `TAG:2752`, `TAG:3110-3115`, `TAG:2741-2744`
60. **Cloud sync has no conflict resolution or merge view** — pull is a full local replace, with a pre-pull backup as the only safety net. — **med / large**. `CLOUD:249-266`, `CLOUD:279-304`, `CLOUD:395-426`
61. **Import is create-only** — rows whose tag or serial already exists are skipped; no "update existing devices from CSV" merge mode. — **med / med**. `TAG:1128-1133`
62. **No integrity/self-check or diagnostics page** — no `PRAGMA integrity_check`, no "rebuild indexes", no validation report. — **low / med**

---

## Suggested triage order

- **Fix now:** A1 (bulk IndexError), A3 (sync fingerprint race), B11 (500 leakage), B20 (viewer export), E45 (schema `user_version` + a handful of API tests).
- **Next:** A2 (audit OFFSET), D35/D36 (search + serial index), F59 (scheduled backups), F52 (warranty dates — best added before more deployments harden the schema).
- **Structural:** E43/E44 (split files; single-source the QR/Code-128 tables with cross-language test vectors).
