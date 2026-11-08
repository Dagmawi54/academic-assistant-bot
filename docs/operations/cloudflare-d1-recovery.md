# Cloudflare D1 Recovery Runbook

This runbook covers the Cloudflare staging database `academic-assistant-college-pilot` and the future college pilot database. It does not store credentials. Use the account's authenticated Wrangler session and keep exported SQL outside the repository in encrypted, access-controlled storage.

## Recovery layers

- D1 Time Travel is enabled automatically. On the current Workers Free plan, point-in-time recovery covers the last 7 days. Check the account's current plan and retention before relying on it.
- A SQL export is the portable recovery copy. Run it before risky schema changes and on the institution's agreed backup schedule. Store copies outside the repository and outside the database's own account when the college requires account-loss protection. No recurring export archive is configured yet.
- The staging export/import drill on 2026-11-04 restored the export into a separate temporary D1 database. The restored copy had all 22 tables, including `class_poll_receipts`, and its `runtime_probe` row. The temporary database and SQL file were removed after verification. The staging database was not restored or overwritten.

## Create a SQL export

From `cloudflare/worker` in PowerShell, choose a protected backup folder that is not inside the repository:

```powershell
$backupPath = 'D:\SecureBackups\academic-assistant-2026-11-04.sql'
npx wrangler d1 export academic-assistant-college-pilot --remote --output $backupPath
Get-FileHash -Algorithm SHA256 $backupPath
```

Store the SQL file and its SHA-256 checksum in encrypted, access-controlled backup storage. Restrict access because exports may contain college and student records. Wrangler exports the full schema and data unless `--no-data` or `--no-schema` is provided.

## Point-in-time recovery

First record a current export and stop incoming writes using the approved maintenance procedure. To find a recovery bookmark, run:

```powershell
npx wrangler d1 time-travel info academic-assistant-college-pilot
npx wrangler d1 time-travel info academic-assistant-college-pilot --timestamp '2026-11-04T16:00:00Z'
```

Review the intended timestamp with the incident owner, then restore:

```powershell
npx wrangler d1 time-travel restore academic-assistant-college-pilot --timestamp '2026-11-04T16:00:00Z'
```

Time Travel restore overwrites the target database in place and cancels in-flight queries. Record the returned previous bookmark so the operation can be undone if needed. Afterward, run the migration list, `/ready`, and application data checks before resuming writes.

## Restore a SQL export to a separate D1 database

Prefer validating an export in a separate database before any recovery. Create a temporary D1 database, import the SQL file, and compare its schema and known records to the export point:

```powershell
npx wrangler d1 create academic-assistant-restore-drill
npx wrangler d1 execute academic-assistant-restore-drill --remote --file $backupPath
npx wrangler d1 execute academic-assistant-restore-drill --remote --command "SELECT COUNT(*) AS table_count FROM sqlite_master WHERE type='table'; SELECT cron_runs FROM runtime_probe WHERE id=1;"
```

Do not attach the restore-drill database to the production Worker. After validation, remove only the temporary database by its exact name using `npx wrangler d1 delete academic-assistant-restore-drill`, then remove the temporary SQL file according to the backup retention policy. A full recovery into the production database needs a maintenance window and an explicit change record.

## Pilot readiness gap

The staging restore drill proves export/import mechanics with the current mostly empty schema and operational probe data. Before a college stores real records, agree on backup frequency, retention, storage location, access owners, restore decision authority, and a recurring restore drill. If the college requires automated long-term exports, configure a scheduled export to a separately protected archive before onboarding it.

## Cloudflare references

- [D1 Time Travel and backups](https://developers.cloudflare.com/d1/reference/time-travel/)
- [D1 import and export](https://developers.cloudflare.com/d1/best-practices/import-export-data/)
- [D1 limits](https://developers.cloudflare.com/d1/platform/limits/)
