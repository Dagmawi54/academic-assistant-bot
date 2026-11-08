# Academic OS Phase 1 Live Telegram Validation

Use this checklist against a real registered Telegram supergroup with active forum topics mapped to courses.

## Preflight

- 2026-11-01: Read-only Telegram `getMe` using the token value read directly from `.env` succeeded for `@academicgroupmanagementbot`. An earlier check returned 401 because a different Windows user-level `BOT_TOKEN` overrode `.env` in that process. `getWebhookInfo` reports no webhook and zero pending updates. No messages were sent. Never paste the token into this document or chat.
- 2026-11-04: Staging Worker `academic-assistant-college-pilot` deployed at `https://academic-assistant-college-pilot.earnlets96.workers.dev`; remote D1 migrations `0001`–`0010` applied and verified. `/health` returns `ok`; `/ready` reports Cron runs. Synthetic webhook auth previously returned 403 for a wrong header and 200/`ignored` for a valid callback update; no Telegram message was sent. Private quiz authoring, membership gating, private scoring, and authorized anonymous class polls are included in the current deployment. The bot webhook remains unset.
- 2026-11-04: Exported staging D1 to a temporary SQL file, imported it into a separate restore-drill D1, and verified all 22 tables plus the runtime probe row. Deleted the temporary database and export after verification; staging was not overwritten. D1 Time Travel is available, but the recurring long-term export archive is not configured.
- 2026-11-08: Migration `0018_tutor_documents.sql` applied to remote D1; Wrangler deployed version `71ac6b99-d84b-4fee-ad9b-0286feee3c23`. Remote migrations report no pending changes. `/health` returns `ok`; `/ready` reports active Cron runs. Read-only Telegram `getWebhookInfo` confirms the live bot webhook still points to `https://academic-assistant-college-pilot.earnlets96.workers.dev/telegram/webhook`, with zero pending updates and no recent webhook error. The webhook target was not changed, and no Telegram message was sent as part of the release checks. The new worksheet batch flow remains unverified in a live chat.

## Environment

- Date/time:
- Bot deployment/runtime:
- Cloudflare Worker name and deployment id (when deployed):
- Group chat id:
- Admin Telegram user id:
- Active semester:
- Database:

## Topic Setup

For Cloudflare pilot onboarding, a group admin runs `/group_id`, starts the bot privately, and runs `/group_setup -100CHATID COLLEGE_INVITE_CODE` privately. The invite code must not be posted in a group. A Telegram admin assigns class reps by replying to a student's message with `/rep_add`; `/rep_remove` removes one. In each forum topic, an admin or assigned rep runs `/course_map Course Name`.

Local validation: group ID formatting, private invite handling (including no invite-code echo), topic mapping dispatch, representative assignment dispatch, and unregistered group resource rejection pass in the Worker test suite. No real Telegram group or Cloudflare deployment has been used for this validation.

| Course | Topic name | message_thread_id | Topic status | Course active |
| --- | --- | ---: | --- | --- |
| Math |  |  | active | yes |
| Data Structures |  |  | active | yes |

## Required Message Checks

For each message, confirm:

- Visible detection acknowledgement appears in the same Telegram topic.
- `AcademicItem` row exists.
- Course is inferred from topic when not present in text.
- Reminder rows exist when a deadline exists.
- APScheduler jobs exist for pending reminders.
- Events dashboard shows the item.
- Scheduled Reminders dashboard shows reminder timestamps.
- Scheduler Jobs dashboard shows `reminder_{id}` jobs.
- Source Telegram link opens the original message.

| Message | Topic/Course | AcademicItem id | Detected type | Inferred course | Reminder ids | Scheduler job ids | Dashboard visible | Ack copied text |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| `exam on monday` | Math |  | exam | Math |  |  |  |  |
| `assignment due friday` | Data Structures |  | assignment | Data Structures |  |  |  |  |
| `covers chapter 1-4` | Math |  | exam_coverage | Math | n/a | n/a |  |  |
| `excluding recursion` | Data Structures |  | exam_coverage | Data Structures | n/a | n/a |  |  |

## Reminder Delivery Check

Create a near-future event in a course topic and temporarily configure a reminder offset that fires during validation.

| Message | Course/topic | AcademicItem id | Reminder id | Expected fire time | Delivered to thread_id | Delivered text |
| --- | --- | ---: | ---: | --- | ---: | --- |
|  |  |  |  |  |  |  |

## Duplicate Suppression Check

Send a duplicate academic message with the same course and similar deadline.

| Original item id | Duplicate message | DuplicateLog id | Dashboard visible | Suppression reason |
| ---: | --- | ---: | --- | --- |
|  |  |  |  |  |

## Private Quiz Check

In a registered group's mapped course topic, an authorized rep creates a quiz, adds questions, and publishes it. Open the deep link as a current group member and confirm each question arrives in private chat, inline answers advance once, and the final score is private. Verify a nonmember cannot see questions and `/quiz_results ID` reports only completed count and average percentage in the group. This staging release has automated tests for storage scoping, duplicate answers, membership rejection, private question delivery, and private completed scores; no class-group quiz has yet been run live.

| Quiz ID | Course/topic | Rep user id | Member test | Nonmember denied | Private score | Aggregate results | Issues |
| ---: | --- | ---: | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |

## Class Poll Check

In a registered test group or course topic, an authorized rep posts a two-option anonymous poll using `/class_poll Question | Option A | Option B`. Confirm the poll appears in the originating topic, Telegram accepts a vote, a non-rep cannot post one, invalid or duplicate options are rejected, and replaying one update ID does not post another poll. The D1 receipt stores poll delivery metadata only; vote data stays with Telegram.

| Group/topic | Rep user id | Poll question | Vote accepted | Unauthorized rejected | Replay suppressed | Issues |
| --- | ---: | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |

## Course-Grounded Tutor Check

The Telegram webhook is currently registered to the Cloudflare staging Worker. Migration `0012` scopes course materials, and migration `0013` supports private-chat rate counters. First confirm private `/start`, `/ask question`, and plain-text tutoring in a private chat. The private tutor answers from the AI model even when live search is unavailable. Then, after `TAVILY_SEARCH_API_KEY` is configured and a test course is mapped with ready materials, ask in that course topic. Confirm the bot says it found the answer in course materials and links the exact original Telegram message; web references should appear separately under “Additional sources.” Test no matching material, provider failure, and a similar source in another group/semester. Neither question text nor answer text should be stored in D1. Keep the webhook on staging during these checks; production group workflows still need a dedicated live group validation.

| Group/topic | Course/semester | Question case | Material source linked | Web sources cited | Scope respected | Query/answer absent from D1 | Issues |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |

## Update Replay Check

Replay the same Telegram update payload with the same `chat_id` and `message_id`. Confirm that it does not create another item, reminder set, duplicate log, acknowledgement, or routed notification.

| Chat id | Message id | First delivery item/reminders | Replay result | Duplicate side effects |
| ---: | ---: | --- | --- | --- |
|  |  |  |  |  |

## Webhook Authentication Check

In webhook mode, confirm that a missing or incorrect `X-Telegram-Bot-Api-Secret-Token` receives HTTP 403 and does not reach the dispatcher. Confirm the configured secret is accepted. Local polling mode does not use this check.

| Case | HTTP result | Dispatcher called |
| --- | ---: | --- |
| Missing secret |  | no |
| Incorrect secret |  | no |
| Correct secret |  | yes |

## Backup and Restore Check

The staging SQL export/import drill passed in a disposable D1 database. Before a college pilot, set the backup schedule and retention, protect exports outside the repository, assign restore ownership, and repeat this drill with representative non-sensitive data. Follow [the D1 recovery runbook](../operations/cloudflare-d1-recovery.md). Time Travel restore runs against the target database in place and needs an approved maintenance window.

## Result

- Pass/fail:
- Issues found:
- Follow-up fixes:
