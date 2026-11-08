# Cloudflare Free Pilot Compatibility Check

**Selected target:** Cloudflare Python Workers + D1 + Cron Triggers  
**Check date:** 2026-11-01

## Platform support confirmed from Cloudflare documentation

- Python Workers can use FastAPI through Cloudflare's ASGI adapter.
- Python Workers can query D1 through a named D1 binding.
- Python Workers support Cron Triggers through a `scheduled` handler.
- Current Workers Free limits: 100,000 requests/day and 10 ms CPU per HTTP request or Cron invocation.
- Current D1 Free limits: 500 MB per database, 5 GB total account storage, 5 million rows read/day, 100,000 rows written/day, 7 days of Time Travel, and 50 D1 queries per Worker invocation.
- The Worker Cron runs every minute. Reminder and digest dispatch are each capped at five per invocation; their worst-case combined path is about 35 D1 queries plus up to 10 Telegram API requests, below the documented 50-request subrequest cap. Real CPU usage is still unmeasured and the 10 ms CPU cap remains the main hosting risk.
- Python Workers use Pyodide. Supported third-party dependencies are pure Python, PyEmscripten wheels, or packages included in Pyodide. Package availability must be checked feature by feature.

Limits were checked against the [Workers limits](https://developers.cloudflare.com/workers/platform/limits/) and [D1 limits](https://developers.cloudflare.com/d1/platform/limits/) documentation on 2026-11-01. Cloudflare can change these limits; recheck them before a paid pilot or launch.

## Project-specific compatibility requirements

- `FastAPI`: supported by Cloudflare's ASGI adapter.
- `aiogram`: not yet verified in the Worker runtime. Its regular client transport may depend on socket behavior unavailable to Workers; use a Workers-compatible Telegram Bot API `fetch` transport if needed.
- `SQLAlchemy` and `asyncpg`: cannot be pointed at a D1 binding as a normal database URL. Production persistence needs a D1 repository adapter or another Workers-supported database interface.
- `APScheduler`: replace reminder timing with a Python Worker Cron Trigger that scans and claims due D1 rows.
- `Redis`: not needed for the first single-college bot flows unless a measured feature requires it; do not retain as a hidden runtime requirement.
- OCR/document parsing: probe each dependency and filesystem assumption. Keep uploaded copies in R2 if retained; do not rely on the Worker's ephemeral filesystem.
- AI APIs: use Workers-compatible outbound `fetch`, then measure execution and subrequest budgets.

## Worker implementation

The Worker lives in `cloudflare/worker/`. It provides FastAPI `/health` and `/ready` endpoints, reads and writes D1 rows, syncs app-shaped academic payloads when configured, and claims due reminders from a one-minute Python Cron handler. It contains no credentials or real student records.

## Local verification status

- Local runtime: Pywrangler with Wrangler 4.156.0 and Pyodide 3.14.2. On this Windows machine, a space-free temporary Python runtime path was needed because the default profile path contains a space.
- The proof's compatibility date is pinned to `2026-10-30`, the newest date supported by the local Workerd binary used for this run.
- D1 migration `0001_initial.sql` applied successfully to Wrangler's local D1 emulator.
- `/health` returned `{"status":"ok"}` and `/ready` returned `{"status":"ready","cron_runs":0}` before Cron dispatch.
- Two local Cron invocations returned `outcome: ok`; `/ready` then returned `cron_runs: 2`. This verifies the Python scheduled handler can write to D1 and the FastAPI handler can read the persisted count.
- After stopping and restarting Wrangler, `/ready` still returned `cron_runs: 2`, confirming local D1 state survived a Worker process restart. Pyodide's first local response took about 107 seconds, the next about 40 seconds, and a later warm response about 15 ms; these emulator startup timings are not a production CPU or latency measurement.
- The first Cron invocation exposed that the local scheduled event passed `env=None`; the handler now falls back to `WorkerEntrypoint.env`. The regression test passes.
- Migration `0002_reminder_outbox.sql` adds a durable reminder outbox with pending/sending/sent/failed/cancelled states, leases, retry timestamps, and attempt counts. A local due reminder was claimed by Cron as `sending` with a lease and incremented attempt count. A second reminder invoked twice within its active lease stayed at attempt count 1; an expired lease was reclaimable on a later Cron run.
- Migration `0003_academic_reminders.sql` adds structured assignment, exam, and quiz rows plus source IDs on reminder rows. The app now has an optional post-commit HTTP mirror into the authenticated D1 endpoint. It preserves group/chat/topic, deadline, room, coverage, status, cancellation, and stable source IDs. Local payload and D1 migration smoke checks pass.
- Migration `0004_telegram_updates.sql` and `POST /telegram/webhook` add constant-time Telegram webhook secret validation, a size limit, and lease-based update idempotency. The first Worker bot flow supports `/start`, `/help`, `/upcoming`, and admin-only structured assignment/exam/quiz commands with Addis Ababa deadlines and D1-backed reminders at 48h/24h/deadline. Worker tests pass and migrations `0001`–`0004` apply in the local D1 emulator.
- Migration `0005_ai_academic_extraction.sql` and the Worker add bounded Groq-based extraction for academic-looking messages, validated output, confidence-based pending review, and admin approval. Parser and routing tests pass; no live Groq request or generated extraction has been evaluated. AI requires a Worker secret and must be benchmarked against real workload and CPU limits.
- Migration `0006_course_resources.sql` adds idempotent Telegram file/link resource indexing, with `/resources keyword` scoped to the current group or topic. Results point to the original message and keep file access controlled by Telegram membership. Local parser/search tests pass; local emulator migration applied.
- Migration `0007_weekly_digests.sql` adds private `/start` registration, explicit `/digest_on` and `/digest_off` subscriptions, weekly D1 delivery records, and retry-safe Cron dispatch every Sunday at 18:00 Addis time. Local schedule, content, subscription, and delivery-lease tests pass; migration applied to local D1.
- The Worker bot slice is not yet a replacement for the current app: private quizzes/results, polls, and a college administration dashboard remain unported. College-scoped group onboarding, topic mapping, and representative controls are implemented but not tested against a live group. Telegram's webhook is still unset; no live bot traffic has been routed to the Worker.
- A Workers-native `fetch` transport now posts reminder rows to Telegram's Bot API with optional forum topic targeting. Confirmed sends transition to `sent`; failures get a sanitized error code and exponential retry delay capped at one hour, with five total attempts. The invalid-destination failure path passed on the live staging Cron; successful delivery still needs a dedicated test chat.
- The app mirror is optional and disabled unless both Worker URL and shared secret are configured. When enabled, a PostgreSQL-backed sync outbox stores each event in the academic transaction and retries failed delivery through APScheduler with capped exponential delay. The app's existing scheduler remains authoritative. Do not enable Worker sends for the same reminders until the app sender is deliberately disabled and the cutover is validated.
- No message was sent through Telegram. Read-only Telegram preflight using the `.env` token succeeded for `@academicgroupmanagementbot`; `getWebhookInfo` still reports no webhook and zero pending updates.
- On 2026-11-01, authenticated Wrangler created remote D1 `academic-assistant-college-pilot` (database ID is in `wrangler.jsonc`, Cloudflare region WEUR) and applied migrations `0001`–`0008`. A remote schema query confirmed the college, group, course, representative, resource, and academic-item tables; Wrangler reports no pending migrations.
- The staging Worker was deployed to `https://academic-assistant-college-pilot.earnlets96.workers.dev` with `* * * * *` Cron. `/health` returned `{"status":"ok"}` and `/ready` reached `cron_runs: 6`; Wrangler tail showed repeated scheduled invocations with outcome `Ok`.
- `BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and `ACADEMIC_SYNC_SECRET` are stored as Cloudflare Worker secrets; their values are not in the repository. A synthetic callback update with an invalid webhook header returned 403; with the configured header it returned 200/`ignored`. This exercised the deployed endpoint without sending a Telegram message.
- Live Cron failure-path check: a due reminder with deliberately invalid `chat_id=0` was claimed once, its Telegram API send failed, and D1 recorded `status=failed`, `attempt_count=1`, and the sanitized `telegram_send_failed` code. The synthetic outbox row was deleted afterward, so no real chat received a message. Successful reminder delivery still needs a dedicated test chat.
- Read-only Telegram `getWebhookInfo` after staging setup still reports no webhook and zero pending updates.
- Telegram's actual webhook remains unset. No real group data, college, or reminders were added, so Telegram delivery and reminder sending are not yet validated. The `ACADEMIC_GROQ_API_KEY` is not configured; AI extraction is disabled.

## Gate before a live pilot

The local platform proof passes for FastAPI startup, D1 migrations, record sync, webhook validation/idempotency, Cron dispatch, and due-row leasing. Retry state for reminders, digests, and app-to-Worker sync plus the Workers-compatible Telegram transport are unit-tested. College setup, topic mapping, and class-rep permission checks are locally tested. Remote D1 and a staging Worker now exist; the HTTP health checks, synthetic webhook validation, and live scheduled tick pass. Next, provision the intended college/group and complete reminder delivery against a safe test chat before considering any production webhook change. Measure CPU and quota use with realistic schedules. Live AI extraction and Telegram deliveries remain unverified.

## References

- [Python Workers](https://developers.cloudflare.com/workers/languages/python/)
- [FastAPI on Python Workers](https://developers.cloudflare.com/workers/languages/python/packages/fastapi/)
- [Query D1 from Python Workers](https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/)
- [Python Worker Cron Triggers](https://developers.cloudflare.com/changelog/post/2025-04-22-python-worker-cron-triggers/)
- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
