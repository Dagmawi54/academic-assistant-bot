# Cloudflare Python Worker pilot

This Worker is the Cloudflare-hosted staging path for Telegram academic groups. Cloudflare Workers runs the webhook and Cron handler; Cloudflare D1 stores college-scoped academic groups, courses, resources, academic records, automatic quiz sources/runs/poll deliveries, tutor memory, short-lived tutor worksheet/follow-up state, legacy private quiz records, update receipts, and durable reminder/digest outboxes. Staging is deployed at `https://academic-assistant-college-pilot.earnlets96.workers.dev`. Telegram's bot webhook currently routes to this staging Worker; the suspended Render service is not the active bot runtime.

## Run locally

From this directory, run:

```powershell
uv run pywrangler dev --test-scheduled
```

In another PowerShell window, initialize the local D1 schema:

```powershell
uv run pywrangler d1 migrations apply academic-assistant-college-pilot --local
```

Check the service and database:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
Invoke-RestMethod http://127.0.0.1:8787/ready
```

Invoke the scheduled handler locally:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8787/cdn-cgi/local/scheduled?format=json'
Invoke-RestMethod http://127.0.0.1:8787/ready
```

The scheduled invocation should return `outcome: ok`, and `cron_runs` should increase. Repeat the invocation to verify that the counter continues increasing. To check restart recovery, stop and restart the dev server and confirm `/ready` still reports the same counter. This is local emulator data under Wrangler's state directory, not an application database.

## Verify due reminder leasing locally

After the dev server is running and both migrations have been applied, insert one already-due sample reminder. This writes only to the local D1 emulator:

```powershell
$due = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - 1000
$sql = "INSERT INTO reminder_outbox (chat_id, thread_id, send_time_ms, message_text) VALUES ('-100test', 42, $due, 'Local reminder test');"
uv run pywrangler d1 execute academic-assistant-college-pilot --local --command $sql
```

Invoke Cron once, then inspect the row:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8787/cdn-cgi/local/scheduled?format=json'
uv run pywrangler d1 execute academic-assistant-college-pilot --local --command "SELECT id, status, attempt_count, lease_until_ms FROM reminder_outbox ORDER BY id DESC LIMIT 1;"
```

The row should be `sending` with `attempt_count` 1 and a lease expiry. The Worker then calls Telegram's Bot API using the `BOT_TOKEN` Worker secret, marks confirmed sends `sent`, or records a bounded retry time after failure. Invoke Cron again immediately; the active lease should prevent another claim. After a lease expires, a later Cron run can reclaim it, which is the recovery path for a Worker that stopped mid-delivery.

The scheduled handler can now send real messages if `BOT_TOKEN` is configured. Never configure a live bot token for this synthetic row; use a dedicated test bot and chat for end-to-end delivery checks. Do not place the token in `wrangler.jsonc` or commit it.

## College and group onboarding

Provision a college record through the authenticated `POST /internal/colleges` endpoint with `college_id` and `college_name`. The endpoint uses the same `ACADEMIC_SYNC_SECRET` as app synchronization. Group setup no longer requires college invite codes.

In Telegram, open `https://t.me/academicgroupmanagementbot?startgroup=setup`, choose the class group, and add the bot. Telegram sends `/start@academicgroupmanagementbot setup` to the group; the Worker verifies the sender is a Telegram group admin, creates an isolated workspace named for that group, and opens guided setup. If needed, an admin can run `/group_setup` in the group. The group admin chooses the active semester, then applies clear course-topic matches or resolves uncertain ones. There is no separate college code or chat ID to copy. A Telegram group admin can assign a class rep by replying to the student's message with `/rep_add`, or remove them with `/rep_remove`. In each forum course topic, a group admin or class rep runs `/course_map Course Name`. Academic items and resource links inherit that course from the mapped topic. `/upcoming` and `/resources keyword` stay inside the current group, and resources preserve Telegram's own access controls by linking to the original message.

## Staging deployment status

Staging D1 database `academic-assistant-college-pilot` is configured in `wrangler.jsonc`; migrations through `0018_tutor_documents.sql` are applied. The Worker is deployed at `https://academic-assistant-college-pilot.earnlets96.workers.dev`. On 2026-11-08 the release completed, `/health` returned `ok`, `/ready` reported active Cron runs, and Wrangler reported no pending migrations. `BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `ACADEMIC_SYNC_SECRET`, and `TAVILY_SEARCH_API_KEY` are Cloudflare Worker secrets, not repository values. `getWebhookInfo` confirmed the live bot webhook still points to this Worker with no pending updates or recent delivery error; the release did not change the webhook. Worksheet upload, batch solve, retry, and follow-up-button behavior still need a dedicated live Telegram test before treating the service as a college production deployment.

D1 Time Travel is automatically enabled; the current Free plan provides 7 days of point-in-time recovery. The export/import restore drill passed against a separate temporary D1 database. There is no recurring long-term SQL export archive configured yet. See [the D1 recovery runbook](../../docs/operations/cloudflare-d1-recovery.md) before onboarding real college data.

The existing Render deployment remains suspended and is not changed by this Worker code. The full project's aiogram handlers, SQLAlchemy data model, Redis, OCR, and document parsing still require migration or a deliberate compatibility decision. Quiz authoring and private results now run in the Worker; polls remain a later checkpoint.

## Academic item sync contract

Migration `0003_academic_reminders.sql` adds a D1 copy of assignment, exam, and quiz records plus stable source IDs on outbox rows. The authenticated `POST /internal/academic-items` endpoint accepts bounded batches from the existing bot, upserts items by source item ID, and upserts reminders by `(source_item_id, source_reminder_id)`. This makes retries safe and preserves `sent`, `sending`, and `failed` delivery states. Cancelled reminders stay cancelled. Reminder text is rendered from the structured fields and HTML-escaped before it enters the queue.

The app can mirror committed assignment, exam, and quiz records to this endpoint when `CLOUDFLARE_WORKER_URL` and `CLOUDFLARE_SYNC_SECRET` are configured. It writes a PostgreSQL-backed sync outbox in the same transaction as the academic change, then sends after commit. Failed requests stay queued with capped exponential retry on the app's one-minute scheduler. The payload includes stable source IDs, Addis Ababa timestamps, group/chat/topic routing, item status, and reminder sent/cancelled state. The mirror remains disabled when those settings are blank. The current app scheduler is still active, so do not configure the Worker to send the same reminders until a deliberate cutover disables the app sender.

Before using the endpoint, set the Worker secret `ACADEMIC_SYNC_SECRET` and the matching app secret `CLOUDFLARE_SYNC_SECRET`. Keep `BOT_TOKEN` unset in the Worker during sync validation; the endpoint can be exercised without sending messages.

## Telegram webhook slice

Migrations `0004_telegram_updates.sql` and `0005_ai_academic_extraction.sql` add update receipts and extraction metadata. `POST /telegram/webhook` checks `X-Telegram-Bot-Api-Secret-Token` in constant time, rejects oversized or malformed updates, and leases update IDs so concurrent Telegram retries do not create repeated academic rows. It supports `/start`, `/help`, `/group_id`, `/upcoming`, class-rep structured event commands, and bounded Groq extraction for ordinary academic-looking group messages:

```text
/assignment Database HW 2 | 2026-11-05 14:30
/exam Midterm | 2026-11-12 09:00
```

Deadlines use Addis Ababa time. Future reminders are queued at 48 hours, 24 hours, and the deadline, routed to the originating group/topic. Only a registered group admin or assigned class rep can add a structured event or approve an AI suggestion; students can read the upcoming list.

AI extraction uses `ACADEMIC_GROQ_API_KEY` (or `GROQ_API_KEY`) and `ACADEMIC_EXTRACTION_MODEL`. It skips ordinary chatter, validates the JSON result, and only auto-activates confidence scores at or above 0.90; lower scores are saved as `new` and require an admin's `/approve UPDATE_ID` command. AI suggestions are not a live-validated replacement for the existing app classifier.

Before a staging deployment, add `BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `ACADEMIC_SYNC_SECRET`, and (if AI extraction is enabled) `ACADEMIC_GROQ_API_KEY` as Worker secrets. Apply all migrations to a real D1 database, configure Telegram group visibility, validate the Worker CPU budget, and complete a dedicated test-group run. Course/topic setup and weekly digests have implementation, but neither has been tested against a live Telegram group. Do not point the production bot webhook here until deployment and the live checklist are completed.

## Course resource search

Migration `0006_course_resources.sql` stores group/topic-scoped Telegram source-message links, captions, external URLs, and Telegram file IDs. New group documents, photos, videos, audio/voice files, and URLs are recorded idempotently by `(chat_id, source_message_id)`. Students can search the current topic with `/resources keyword`; from the group general topic, the search covers the group. Results link to the original Telegram message so access permissions remain with the group. The Worker does not download or copy the files. Course names are inherited from a mapped forum topic; unmapped material remains topic-scoped.

## Course-grounded web tutor

Migration `0012_course_tutor_search.sql` adds passage indexing for text extracted by the automated course-material pipeline. Migration `0013_private_tutor_chats.sql` lets private chat usage counters cover Telegram DMs as well as registered groups. Migration `0014_tutor_memory.sql` adds durable D1 storage for explicitly saved study preferences and recent tutor exchanges. The Worker indexes ready PDF, image, and representative-marked note content into D1 FTS5, then searches only the course mapped to the invoking forum topic and the group's active semester. Passage rows retain the source message link; the Worker returns a small ranked set to the answer model rather than the full course inventory. The Cron handler indexes at most one queued source passage set per tick, before quiz work.

Students can chat privately with the academic assistant using plain text or `/ask your question`. Responses use the Cloudflare Workers AI model, match the student's tone, and use Telegram formatting, including language-tagged code blocks. Private chats accept PDF, common image, Word, PowerPoint, spreadsheet, CSV, HTML, and XML attachments up to Telegram's 20 MB download limit. Cloudflare Markdown Conversion extracts document text and describes supported images; `.pptx` slide text is extracted directly from the presentation package. The tutor answers the caption or summarizes the attachment if no caption is supplied. Extracted attachment contents are sent to Workers AI for that response and are not retained in D1 or sent to Tavily. The bot refreshes Telegram's typing indicator while preparing a response. Private answers use optional live web results when the student asks for current or cited sources; they do not search or infer a college course. In a mapped forum topic, `/ask your question` searches only that group's mapped course and current semester. Course answers clearly say when material was found, link the original Telegram message, and add a separate **Additional sources** section for web results. Course explanations are limited to 2,800 characters so provenance and source links fit in Telegram. If no course material matches, the answer says so. Search failure is disclosed; the private assistant still answers from general academic knowledge, while course answers fall back to course material when available. Limits are 30 requests per user per UTC day, 100 requests per chat per UTC day, and 3,000 tutor requests per UTC month. D1 stores up to the latest eight tutor exchanges per user, chat, and context (private chat or a mapped course topic and semester); tutor study preferences are saved only when the user asks. Use `/remember preference` to set a preference, `/memory` in private chat to view it, and `/forget_me` to erase all tutor conversations and preferences for your Telegram account. Normal chat does not infer or save personal facts. On `/start`, the Worker registers separate private-chat and group-chat command lists so Telegram's built-in Menu button shows the relevant actions. The welcome and help messages also provide inline buttons for common actions.

The live search adapter calls Tavily with only the student's question. It does not send course excerpts, college/group names, Telegram IDs, or source links to Tavily. Add the provider key to each Worker environment as a secret; do not put it in `wrangler.jsonc`, `.env.example`, or source control:

```powershell
npx wrangler secret put TAVILY_SEARCH_API_KEY
```

If the secret is absent or the provider quota is reached, live search is not configured and the Worker gives a clearly labeled general-knowledge answer in private chat or a course-material-only answer in a mapped course topic. Check Tavily's current plan and usage allowance before enabling the key for a college pilot.

## Weekly study digest

Migration `0007_weekly_digests.sql` adds explicit private-bot starts, group/topic subscriptions, and a durable digest delivery outbox. A student first opens the bot privately and sends `/start`, then opts in from a class group with `/digest_on`; `/digest_off` stops future digests. The Worker prepares a summary of the next seven days every Sunday at 18:00 Addis Ababa time. Delivery is private, topic-scoped when the opt-in command came from a forum topic, and retried from D1 after transient send failures. No group member is subscribed automatically.

## College scope and permissions

Migration `0008_college_group_setup.sql` adds colleges, the legacy invite-code table, registered groups, courses, topic mappings, and class representatives. The legacy table remains in existing D1 databases but current onboarding does not create or consume invite codes. New groups receive an isolated workspace only after their Telegram admin is verified. Group admins control rep assignment; admins and assigned reps can record and approve event dates. Queries join resources and academic items to the active registered group and its college before returning data.

## Legacy private quizzes

Migration `0009_private_quizzes.sql` remains for data compatibility. New private quizzes cannot be created with `/quiz_create`; use the automatic topic-poll flow below. Existing quiz tables are not deleted.

## Automatic course Quiz polls

Migration `0011_automated_topic_quizzes.sql` adds current-semester course offerings, dedicated quiz-source text, extracted material, the Quiz destination, generation runs, question records, and durable poll deliveries. The Cloudflare AI binding handles PDF/image-to-Markdown conversion and structured question generation; no separate model-provider secret is required for this flow.

Set up each group once:

1. In the class group, a Telegram group admin sends `/semester_set Fall 2026` (use the group's actual term label).
2. In each course's forum topic, a class rep or admin sends `/course_map Course Name`.
3. In the dedicated forum topic named `Quiz`, a Telegram group admin sends `/quiz_topic`.
4. Reps can add a standalone text note by replying to it with `/quiz_source` in the mapped course topic.
5. A group admin can inspect setup, extraction, and the latest run with `/quiz_status`.

PDF documents and common image formats uploaded in mapped course topics are queued for extraction. Telegram's hosted Bot API limits downloads to 20 MB. The Worker keeps extracted text and metadata in D1 and does not persist downloaded bytes. Cloudflare image conversion describes images with AI, so it is best-effort OCR; low-text or unreadable scans are skipped. A course needs at least 1,500 usable extracted characters before quiz generation.

On alternating local calendar days at 10:00 Addis Ababa time, the Worker chooses the eligible course least recently quizzed and prepares five questions. At 16:00 it prepares up to 20 questions for each eligible exam scheduled the next day, using that course's current-semester materials. It posts a short header and native anonymous Telegram Quiz polls directly in the configured Quiz forum topic. Telegram exposes answer correctness and aggregate poll results in the group; this flow does not retain individual voters or votes.

The Worker bounds this deployment to 25 document extractions and 10 quiz-generation calls per UTC day. Cloudflare's own Workers AI allocation is also account-wide; if that allowance is exhausted, the operation pauses/fails and `/quiz_status` reports the saved source/run state. Quiz sends proceed one message per Cron tick; an interrupted or ambiguous Telegram send is marked uncertain and is not replayed automatically.

Add the bot as a group admin so Telegram delivers course-material messages and the Worker can reliably verify group admins. Grant only the message and poll permissions needed for posting quizzes; the bot does not need permission to manage forum topics.

The Worker is configured to use one-minute Cron ticks and the `AI` binding in `wrangler.jsonc`. Before the automatic quiz feature is enabled in staging, apply migrations through `0011`, deploy the Worker, and use `/quiz_status` in a dedicated authorized test group. Before the course-grounded tutor is enabled in staging, apply migration `0012`, configure `TAVILY_SEARCH_API_KEY`, deploy the Worker, and complete the tutor section of the live-validation checklist. Do not change Telegram's production webhook as part of staging validation.

## Personal tasks and reminders

Migration `0016_personal_tasks.sql` stores students' private tasks, one-time reminder delivery state, and timezone preferences in D1. It does not use a local database or mix personal task data with college records. In a private chat, students can write a request such as “Remind me to submit the lab tomorrow at 5 PM,” or use **My tasks** and **Add reminder** from the bot's welcome buttons. `/tasks` lists tasks; buttons mark them done, rename, reschedule, cancel, or delete. `/timezone Area/City` sets an IANA timezone; the default is `Africa/Addis_Ababa`. `/forget_tasks` asks for confirmation before removing all of that user's tasks.

The AI parser proposes an action and explicit zoned timestamp; validation rejects unclear dates, past reminders, invalid timezones, and ambiguous daylight-saving transitions before writing. Clarifications do not create partial tasks. Scheduled reminders run on the existing minute Cron and are sent only to the owning user's private chat. A definite Telegram rejection can be retried; if the send outcome is ambiguous, delivery is marked uncertain to avoid normal duplicate reminders.

## Guided semester and course setup

Migration `0017_guided_group_setup.sql` records forum topic titles seen by the bot, setup runs, and mapping changes. After a class group is linked, an admin or registered representative sets the active term with `/semester_set Fall 2026`, then runs `/group_setup` in the group. The bot shows exact topic/course matches and asks for a button choice when a match is uncertain. Only exact, unique matches for active courses in the selected semester can be auto-applied. Each mapping is audited and **Undo latest setup** restores or removes those changes only while the topic mapping has not changed since.

Telegram bots do not get a listing of all forum topics created before they observed them. The Worker saves topic-created and topic-edited service events it receives after the group is linked. For older topics it has not observed, an admin or representative must open that topic and use `/course_map Course Name`; sending an ordinary message does not recreate the topic-created event. Group linking now uses the Telegram `startgroup=setup` link or `/group_setup` inside the group; no chat ID or college invite code is needed. Apply `0016` and `0017` through the standard D1 migration workflow before deploying this code to a database; these migrations are not applied automatically by the application.

## Worksheet solving and tutor reply controls

The reviewed worksheet patch adds `0018_tutor_documents.sql` because staging already uses `0016_personal_tasks.sql` and `0017_guided_group_setup.sql`. Do not apply the ZIP's original `0016_tutor_documents.sql`; applying it at that version would collide with the deployed schema. After applying `0018`, private uploads with at least three parsed questions plus worksheet cues are saved per student and chat for 24 hours. A clear solve caption can also select questions from a parsed sheet. An uncaptioned worksheet is solved question by question; a caption such as “do them” starts all questions, a caption such as “do question 5” selects that question, and “summarise this” keeps the normal file-summary flow. A non-solving caption is treated as the student's request about the attachment, while a worksheet remains available for a later “do them” message. Numbered lecture notes without exercise cues are not auto-solved. Uploaded document text and follow-up button prompts are deleted by the daily Cron cleanup after expiry and by `/forget_me`; file bytes are not retained.

Each worksheet solution is sent as a new, labelled Telegram message. Long tutor answers keep their first part visible in the progressive draft, and only length-stopped answers get a Continue button. Successful private tutor replies can add three answer-specific follow-up buttons; generating those suggestions makes one additional small Workers AI call, so it may add response time and AI usage. Worksheet and follow-up state is stored in D1, not local Worker storage.

This code and migration are prepared locally but are not deployed or live-tested. For staging rollout, first apply migration `0018` to D1, then deploy the Worker, then verify with a dedicated test bot/chat using (1) a 12-question PDF, (2) “do them”, (3) “continue”, (4) “do question 5”, and (5) an ordinary study question with follow-up buttons. Compare Telegram logs if any bubble is missing. Keep the production bot webhook unchanged until those checks and the remaining operational review pass.

## Class polls

Migration `0010_class_poll_receipts.sql` records only the Telegram update, target group, send status, and returned poll ID. A registered group admin or class rep can post `/class_poll Question | Option A | Option B [| more options]` in a class group or forum topic. Polls are anonymous, single-choice Telegram polls with two to ten distinct options. The Worker does not store vote identities or answers. A unique receipt prevents webhook retries from posting duplicate polls; if Telegram's result is uncertain, retry the command as a new message after checking the group.
