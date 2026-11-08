# Telegram College Academic Assistant — Design

**Date:** 2026-11-01  
**Status:** Approved in conversation; awaiting written spec review  
**Audience:** Students and class representatives at a college pilot

## Goal

Turn the existing Telegram academic group bot into a dependable student support product that can be piloted at a college and later sold to colleges. Keep the first interface inside Telegram. Deliver the agreed feature set through small, verifiable checkpoints so the current reliability problems do not spread into new features.

## Product scope

### Student workflows

- See upcoming assignments and exams and receive deadline reminders.
- Find course resources shared in the relevant Telegram group or course topic.
- Receive a weekly study digest.
- Practice with quizzes and receive results privately.

### Class representative workflows

- Post and correct official academic items.
- Share course resources.
- Run simple class polls.
- Review academic items detected by the bot and correct the course or deadline when needed.

For AI-detected items, show the extracted course and date alongside a link to the source Telegram message. Students should be able to distinguish bot suggestions from class-representative-confirmed information.

Grades and formal attendance tracking are outside this pilot. The Mini App is a later phase.

## Delivery checkpoints

1. **Reliability foundation:** Repair the existing assignment and exam capture flow, deadline reminders, startup and database behavior, and duplicate handling. Verify that items retain their course/topic and source-message context.
2. **Study resources:** Add durable course resource handling and search, then the weekly digest.
3. **Practice and class coordination:** Add quizzes with private student results, then class-representative controls and polls.
4. **College pilot readiness:** Validate permissions, data separation, webhook security, recovery, backup/restore, and all user journeys in real Telegram groups.
5. **Later:** Design and build a Telegram Mini App if the pilot shows that students need a richer interface.

Every checkpoint needs automated regression coverage for its core behavior and a real-group validation checklist. Do not advance a workflow to the college pilot while its required checks fail. The existing `docs/live-validation/academic-os-phase-1.md` checklist is a starting point and must be completed and expanded as part of pilot validation.

## Architecture

Keep the product as a modular monolith. The selected free pilot runtime is Cloudflare Python Workers with FastAPI, D1, and Cron Triggers. Preserve pure Python academic rules where they work, but put platform-specific behavior behind clear adapters: D1 access through the Workers binding, Telegram Bot API calls through Workers-compatible `fetch`, and reminder dispatch through the scheduled handler. Do not assume the existing aiogram, async SQLAlchemy, Redis, APScheduler, or native OCR stack runs unchanged in Pyodide. Avoid a microservices rewrite for the pilot.

The production database must be hosted and persistent; do not deploy with an app-local SQLite file. For a zero-cost pilot, use Cloudflare Python Workers with D1: D1 is managed and persistent, supports point-in-time recovery, Python Workers expose D1 bindings, FastAPI is supported through the ASGI adapter, and Python Cron Triggers can dispatch scheduled work. The deployed Worker must use a D1-backed repository adapter, not an app-local database or an assumed SQLAlchemy connection. The current FastAPI app can provide the HTTP surface, but aiogram's HTTP transport, async SQLAlchemy, Redis, APScheduler, and native OCR/document-processing dependencies need compatibility work or replacement for the Workers runtime. Validate these boundaries in a local Worker proof before porting business features. Free plan CPU is limited to 10 ms per HTTP request and Cron invocation, so measure representative webhook and reminder batches before using it for a live college pilot. Keep hosted PostgreSQL and a paid Python service as the upgrade path if those limits or institutional requirements require it. Telegram Serverless is not the selected hosting target. Add a college ownership boundary to the data model and enforce it in group, course, resource, quiz, and reminder queries so data cannot cross college boundaries. Start with one college pilot while keeping the model ready for more than one college.

Persist reminders and their delivery state in the selected hosted production database. On restart, reconcile pending reminder records with the scheduler. Processing Telegram updates must be idempotent so Telegram retries cannot create duplicate academic items, reminders, quiz submissions, or poll actions.

Keep files in durable object storage when the application retains uploaded copies. Telegram messages and file IDs may be used as source references, but they are not the application database or a replacement for application-owned durable storage.

## Hosting and Telegram boundary

Telegram Serverless (currently presented as beta in BotFather) can host JavaScript bot handlers, Mini App endpoints, a persistent per-bot SQLite-backed database, and Mini App static files on Telegram infrastructure. This can remove the need for an externally hosted bot process and Mini App website. The runtime is a constrained V8 environment: application code must be JavaScript, cannot import npm packages, cannot use a filesystem, and accesses outbound HTTP through Telegram's SDK. Database relationships have no foreign-key enforcement.

The existing project is a Python FastAPI + aiogram application. Moving it to Telegram Serverless would be a port/rewrite of bot behavior and persistence, not a configuration change. It currently relies on Python libraries, APScheduler, Redis, OCR and document parsing packages, and SQLAlchemy. External AI/OCR services could still be called over HTTP, but their availability and costs remain external dependencies.

The reminders feature needs scheduled delivery. I inspected the open BotFather Serverless handler controls: the available handlers correspond to Telegram update types, and there is no scheduled-job or timer control. The Serverless docs likewise do not document cron, scheduled jobs, or timer APIs. Therefore the current evidence does not support scheduled reminder or weekly digest delivery from Serverless alone. Before choosing an all-Telegram deployment, Telegram would need to expose and document a supported scheduled trigger; otherwise keep a separate scheduler or use the current external Python service. The Serverless database documentation describes persistence, but the reviewed docs do not specify backup/restore guarantees or a database export workflow. Resolve that before storing college pilot data there.

The current Render deployment files remain a viable external-hosting path. For production, use a Render web service with paid managed PostgreSQL and durable object storage rather than the container filesystem. Render's free web services spin down after 15 minutes without inbound traffic, and its free PostgreSQL expires after 30 days with no backups, so that combination is suitable only for temporary experiments, not a college pilot. Use Telegram webhooks for production and validate the webhook secret header. Keep polling for local development. A future Mini App may either use Telegram Serverless hosting or an external HTTPS host; if Serverless is selected, its Mini App endpoints receive platform-validated Telegram identity.

### Selected recommendation

Use Cloudflare Python Workers + D1 for the free pilot. Keep reusable pure Python business rules and supported FastAPI routes; use a D1 binding adapter for persistence, a Workers-compatible HTTP `fetch` transport for Telegram Bot API calls, and a Worker `scheduled` handler with Cron Triggers for reminders and digests. Validate each adapter and dependency in a local Worker before porting that feature. Defer native-binary OCR/document workflows or call an external provider if needed. Cloudflare's free plan has finite daily request, database read/write, storage, and CPU limits, including only 10 ms CPU time per free HTTP request or Cron invocation, so measure representative workloads and treat the free tier as a constrained pilot. Keep managed PostgreSQL and a paid Python service as the upgrade path for scale or institutional requirements. No app-local database is allowed in deployed environments.

## Reliability, security, and operations

- Treat a failed AI extraction as recoverable: preserve the original message, report that automatic detection failed when appropriate, and allow a class representative to enter or correct the item.
- Keep reminder delivery state durable and retry transient delivery failures without sending duplicates.
- Apply college and Telegram role checks before viewing or changing academic items, resources, quiz content, or polls.
- Keep student quiz results private to the student and authorized class representatives.
- Validate webhook requests, keep bot and provider secrets in deployment environment settings, and avoid logging secrets or unnecessary message content.
- Separate liveness from readiness: report service health and database connectivity clearly. Log failures with enough IDs and context to diagnose them without leaking credentials.
- Configure database backups or exports and document a restore procedure before the college pilot.

## Pilot acceptance criteria

- A class representative can create or correct an assignment or exam with course, deadline, and source context.
- A matching Telegram group message creates at most one academic item and its expected reminders, including when Telegram retries delivery.
- Pending reminders survive process restarts, are delivered to the correct course group/topic, and do not send twice.
- Students can find resources for a course without seeing resources from another course or college.
- The weekly digest contains the correct upcoming items for the student's course context.
- Quiz attempts and results are private to the student, while class representatives can manage quiz content.
- Poll and class-representative controls reject unauthorized users.
- Real Telegram validation covers detection, correction, resource search, digest, quiz, poll, reminder delivery, and recovery after restart.
- The production app uses a hosted persistent database and durable storage for retained files, with a proven backup/export and restore procedure recorded.

## Key trade-offs

- A modular monolith keeps deployment and debugging manageable for a small pilot while retaining clear seams for later growth.
- College scoping adds some schema and query work now but reduces the risk of data leakage and avoids a disruptive retrofit if more colleges join.
- Staged delivery takes longer than a one-shot feature bundle but creates usable checkpoints and makes failures easier to isolate.
- The Bot-first interface keeps the pilot accessible in existing class groups. Telegram Serverless could host both the bot and a later Mini App, but its runtime and database constraints must be checked against reminders and college data requirements first.

## References

- [Telegram Bot API: webhooks](https://core.telegram.org/bots/api#setwebhook)
- [Telegram Mini Apps](https://core.telegram.org/bots/webapps)
- [Telegram webhook guide](https://core.telegram.org/bots/webhooks)
- [Render free instance limitations](https://render.com/docs/free)
- [Cloudflare Python Workers](https://developers.cloudflare.com/workers/languages/python/)
- [Cloudflare D1 free limits and pricing](https://developers.cloudflare.com/d1/platform/pricing/)
- [Cloudflare D1 point-in-time recovery](https://developers.cloudflare.com/d1/reference/time-travel/)
- [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
