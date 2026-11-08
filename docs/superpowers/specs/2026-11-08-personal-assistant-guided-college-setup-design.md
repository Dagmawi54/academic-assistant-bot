# Personal Assistant and Guided College Setup

**Status:** Approved for written-spec review; implementation has not started.  
**Date:** 2026-11-08

## Purpose

Extend the Telegram bot from a study tutor and group deadline helper into a useful student assistant. The first implementation slice adds private one-time task reminders and makes college group setup more guided, while preserving the existing tutor, course, reminder, and quiz flows.

The design uses the existing Cloudflare Worker, D1 database, Telegram bot, and scheduled trigger. It does not add a separate runtime or connect a user's Telegram account.

## Current project context

- Tutor preference storage is one free-text `study_preferences` field per Telegram user. Named, switchable personalities are not present.
- The Worker already has a durable D1 reminder outbox with leases and retries, plus a scheduled handler that dispatches due reminders.
- Group setup currently links a group to a college through a private invite-code command. Admins set a semester and map a topic with `/semester_set` and `/course_map`.
- Course mappings and current course offerings are stored in `group_topics`, `courses`, `academic_groups.current_semester`, and `course_offerings`.
- The Telegram Bot API does not provide an operation to list all existing forum topics. Telegram's `messages.getForumTopics` method belongs to the user-client API and is marked for users only. Therefore a bot-only setup cannot discover every historical topic by itself. It can use topics already present in its database and record new topic-created events going forward; existing unmapped topics require admin action.
- Visual lesson pages currently use the configured Worker base URL followed by an opaque random lesson ID. The source code does not insert an email into that path. The optional Telegram Instant View wrapper is controlled by `TELEGRAM_IV_RHASH`, which is empty in the current Wrangler configuration. The reported email exposure remains a separate privacy investigation; this design does not assume its cause.

## Approaches considered

### 1. Extend the current Cloudflare bot (recommended)

Add bounded D1 task/reminder records and a guided setup module to the existing Worker. Reuse Telegram, the current D1 deployment, cron delivery patterns, invite flow, and course tables. This keeps the free-tier deployment and operational model simple. Existing forum topics that the bot has never observed still need an admin to identify/map them.

### 2. Add a Telegram user-account integration

An authenticated Telegram user client could enumerate topics unavailable to bots. This adds a user session credential, a second runtime or service, access-control and rotation work, and greater privacy risk. It is not recommended for the college pilot.

### 3. Keep setup manual

Retain the existing commands and add only personal tasks/reminders. This is the smallest change, but leaves administrators with today's fragmented setup steps and does not provide the requested guided flow.

## Phase-one scope

### A. Private tasks and one-time reminders

- Students create tasks by writing a clear natural-language request in a private chat, for example: “Remind me to submit the lab tomorrow at 5.”
- A task can have an optional due/reminder time. The first slice supports one-time reminders only; it does not sync to an external calendar or create recurring schedules.
- Addis Ababa is the default timezone. Each student can set a different timezone. Interpret future natural-language dates in that user's timezone and store scheduled instants consistently. Changing a timezone affects future interpretation/display; it does not silently move an already-scheduled instant.
- For an explicit and unambiguous request, save the task and reply with its title and scheduled local time. If the date, time, or intended action is ambiguous, ask a short follow-up and do not save until clarified.
- Provide a private task view with inline actions to mark complete, edit/reschedule, cancel, or delete. Provide a clear way to delete all personal tasks. A student's task contents and reminder text are sent only to that student's private Telegram chat.
- Use durable database state and the existing scheduled delivery/retry pattern. Stable task IDs and unique update keys prevent duplicate task creation; leases prevent overlapping cron invocations from claiming the same reminder. A definite send failure is retryable; a cancelled or completed task must not send later reminders. If Telegram's response is lost after a send may have succeeded, mark delivery uncertain rather than blindly sending a duplicate.
- Do not mix personal task records with college `academic_items` or group reminder records. This keeps personal and class data scopes distinct.

### B. Guided college group setup

- Preserve the current private invite-code flow for linking a class group to a college and the existing Telegram-admin checks.
- Provide an admin-started setup flow that reports the known college link, active semester, mapped course topics, and missing course offerings from D1.
- If there is no active semester, ask the admin to choose or enter one. Do not guess a semester from the calendar. Use the selected semester to scope course matches.
- Learn topic IDs and titles from topic-created service updates when Telegram supplies the needed identifiers, and use already-mapped topics in D1. For older topics not known to the bot, ask the admin to run the mapping action in that topic or supply its course name; explain this limitation in the setup flow.
- When an authorized admin starts the flow, automatically apply only deterministic clear matches: a normalized topic title or course code exactly matches one active course offering for the selected semester. Approximate or AI-generated suggestions are never auto-applied. For uncertain or conflicting matches, show candidate course names and require an admin selection.
- Record the actor, prior mapping, resulting mapping, and setup run for each automatic change. Show a concise completion report and an undo action for the latest setup run. Undo must restore the prior mapping or remove a newly-created mapping without modifying unrelated semester or course records.
- Do not store or use an admin's personal Telegram account session.

### C. Assistant interaction and controls

- Add menu/button actions for “My tasks,” “Add reminder,” and “Group setup” where appropriate. Keep text commands available for accessibility and direct use.
- Interpret natural-language reminder requests with the existing AI service, but treat model output as an untrusted proposal: validate the task title, date, timezone, and requested action before any database write.
- When a request is ambiguous or cannot be parsed safely, ask the student to clarify instead of inventing a deadline.
- Require the Telegram group administrator or registered representative checks already used by the worker before applying class setup changes. Private tasks are available only to their owner.

## Data and runtime design

- Add a dedicated `personal_tasks` model and indexes in D1 keyed by Telegram user ID and due time. Store a stable task ID, task text, optional due instant, status, timestamps, and an optional one-time reminder relation.
- Add a `personal_task_reminders` delivery record with scheduled instant, status, lease/retry fields, and a uniqueness key per task. This keeps private reminder delivery separate from class-group `reminder_outbox` records while reusing its lease/retry approach.
- Add a small `assistant_user_settings` record for timezone instead of overloading tutor preferences. Default unset timezones to `Africa/Addis_Ababa`; validate user-selected values as supported IANA timezone names.
- Add setup-run/change records sufficient to show applied mappings and reverse the latest run safely. Reuse the existing college, group, course, semester, and topic records.
- Keep task parsing, task CRUD, reminder delivery, and group setup in focused service functions; keep Telegram routing and callback presentation in the Worker entry point.
- Use the existing scheduled Worker dispatch and D1 lease/retry conventions. Do not introduce local storage or an in-memory scheduler as the source of truth.

## Error handling

- If Telegram delivery fails, preserve the task and retry delivery using bounded backoff; report a delivery problem only through the private chat when possible.
- If D1 is unavailable, do not claim that a task was saved or a group was configured.
- If topic history is unavailable, show the observed/mapped data and ask for the missing topic through the guided flow.
- If course-name matching is ambiguous, do not auto-apply it.
- If an undo is stale or another admin has since changed the mapping, refuse to overwrite the newer change and show the current mapping.

## Acceptance criteria

1. A student can create a one-time private reminder in natural language; the bot stores it durably and sends it at the requested local time.
2. Ambiguous date/time requests cause a clarification exchange and no task write until resolved.
3. A student can list, complete, reschedule, cancel, and delete their own tasks; one user's commands cannot alter another user's tasks.
4. Default timezone behavior is `Africa/Addis_Ababa`; a user's configured timezone is used for subsequent input and displayed times.
5. Duplicate updates and overlapping scheduled handlers do not create duplicate tasks or normally duplicate sends. A send with an ambiguous network outcome is marked uncertain instead of automatically repeated.
6. An authorized admin can start group setup, review known semester/course/topic state, auto-apply only unambiguous matches, resolve uncertain matches, and undo the latest setup changes.
7. The setup flow states that the bot cannot list historical forum topics it has never observed, and gives the admin a workable per-topic mapping path.
8. Existing group assignment/exam reminders, tutor memory, course-scoped search, automated quizzes, and file analysis continue to use their current data scopes.

## Deferred roadmap

Keep the following ideas in later slices rather than combining them with phase one:

- Multiple named personalities that can be added, selected, edited, and removed without overwriting the user's base preferences.
- Long-lesson checkpoints that pause after a section, ask whether the student understands, and continue or simplify based on the reply.
- Real, topic-relevant image groups or generated explanatory visuals with explanatory captions. Do not render plain text as an image. Prefer properly sourced images and use generated visuals when no suitable real visual exists.
- Audit the lesson-link email exposure report, verify the exact visible URL/source, and then select Telegram Instant View or a clean custom domain. Never put a user's email in a lesson URL or public visual page.
- External calendar synchronization and recurring personal schedules.

## External API references

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram `messages.getForumTopics` method](https://core.telegram.org/method/messages.getForumTopics)
