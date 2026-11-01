# Upgrade / deploy runbook (private_database bot)

Proven procedure from the Phase 9 deployment (2026-10-27) on the Galaxy
(`~/private_database`). Use it for every future code upgrade. The bot is
never replaced wholesale: files deploy one by one, each with a rollback
copy, and the live database is never touched by a code deploy.

## 0. Preconditions (all must hold before staging anything)

- Production bot is **idle**: `bash status.sh` shows no running job, and
  the jobs table has zero rows with `status IN ('running','queued')`.
  Never deploy while a job runs — a restart mid-job interrupts it (it
  will auto-requeue on boot, but the interruption is avoidable).
- You have the exact list of changed files with relative paths
  (e.g. `app/telegram/bot.py`, `docs/STATUS.md`, `tests/test_er.py`).
- Test files and docs never require a restart (the running bot does not
  import them), but they still deploy with `.bak` + hash verification
  so the deployed tree always matches the proven tree.
- Never run a second bot instance on the production token. Never copy a
  test database over the production database. Never read `.env` values
  during a deploy.

## 1. Stage

Copy every changed file into a staging dir preserving relative paths,
e.g. `~/phase10/deploy/`, and write `MANIFEST.txt` there — one relative
path per line, in deploy order. The staging dir is the single source of
truth for the deploy; nothing deploys except what is listed.

## 2. Deploy (per file, with rollback copy)

For each path in `MANIFEST.txt`:

1. If the file already exists in production, copy it to
   `<file>.bak-YYYYMMDD-auditfix` (same suffix family every time; if a
   `.bak` with the current suffix already exists, keep it and note it —
   do not silently overwrite a previous rollback copy).
2. If the file is new, log `NEW-FILE <path> (no .bak)` — there is
   nothing to roll back to, which is expected.
3. Copy the staged file over the production file.
4. After all copies, verify: `sha256sum` of every deployed file must
   equal the staged file. Any mismatch aborts the deploy (restore from
   `.bak` and investigate; do not restart on a half-deployed tree).
5. `python3 -m py_compile` every deployed `app/**/*.py` file.

Reference implementation: Phase 9's `deploy_prod.sh` (same steps).

## 3. Restart (graceful, verified)

1. `bash stop.sh` — waits up to 60 s for the worker to finish, then
   falls back to `kill -9`. Confirm the old process is gone.
2. `bash start.sh` — records the new pid.
3. `bash status.sh` — bot must report running.
4. Read `logs/stdout.log`: expect `started (pid=<new>, workers=1,
   resumed_jobs=0)` and `bot polling started`, the bot username, and
   **zero errors after the restart line**.
5. Verify data is untouched: file count, record count, schema version
   (`migrations` = 10 at Phase 10 time), FTS5 available, 0 active jobs.
   Compare against the counts recorded in step 0 — they must be
   identical (a code deploy never changes record counts).

## 4. Rollback (per file or whole deploy)

For any file: `cp <file>.bak-YYYYMMDD-auditfix <file>`, then repeat
step 3. For a whole deploy, roll back every file in `MANIFEST.txt`
in reverse order, then restart and re-verify step 3.5. A rollback is
always available because step 2.1 runs before any overwrite.

## 5. Test dependencies (install.sh step 5.5)

The suite needs `pypdf` + `openpyxl`, declared in
`requirements-test.txt`. `install.sh` step 5.5 installs them best-effort
(`pip install -r requirements-test.txt`); its failure does not stop the
install, but the suite will then fail at collection on a fresh device.
Verified 2026-10-27 (Phase 9 and Phase 10, Galaxy): from a fresh copy,
`pip install -r requirements-test.txt --target .testdeps` installs
pypdf 6.21.2 + openpyxl 3.1.5 and both import cleanly. The script's
literal system-pip invocation was deliberately not run on the
production phone (to avoid modifying its system python); the
`--target` run proves the requirements file resolves and installs.
Run the suite with `PYTHONPATH="<deps>:<tree>"` when using `--target`.

## 6. What this runbook does NOT cover

- Database restores (`python -m app.cli restore`) — terminal-only,
  replaces the DB, and always requires a preserved prior copy plus
  explicit approval. See `docs/STATUS.md` (restore is terminal-only
  by design; there is no Telegram button for it).
- Wiring `app/ai/organize.py` to live data — it is standalone and
  unwired by design (its payloads are PII-masked since Phase 9, but
  sending data externally still needs the user's explicit order).
- The `idx` handler — orphan by design (admin-protected, no button);
  it is not dead code to delete.
