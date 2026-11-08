# Automated Course Topic Quizzes — Design

**Date:** 2026-11-04  
**Status:** Approved by user; implementation complete locally, staging deployment pending
**Audience:** Students, class representatives, and admins in the college pilot

## Goal

Automatically generate course-specific Telegram Quiz polls from the course's own forum-topic materials and publish them in the group's dedicated `Quiz` forum topic. Students answer directly in Telegram; no private-chat link or redirect is used.

## Current state

The legacy Python bot has a bi-daily random-course quiz at 10:00 and a daily exam-prep job at 16:00 for exams the next day. It does not consistently filter course choices by the group's current semester, uses `AcademicItem.raw_text` for material/exam-coverage content, and can fall back from a missing Quiz topic to the general topic. The Render service is suspended.

The Cloudflare staging Worker currently supports rep-authored private quizzes. D1 stores the current semester on neither `academic_groups` nor `courses`; resource rows store captions and Telegram file IDs but not extracted document text. This design replaces the private rep-authored flow for new quiz delivery. Keep migration `0009_private_quizzes.sql` and its tables for compatibility; do not use them for the new automatic poll flow.

## Approved interaction

- Post native anonymous Telegram Quiz polls directly in the group's configured Quiz forum topic.
- Each question has four options and one correct answer. Telegram reveals the answer and group poll results through its native quiz UI; student results are not private.
- Reps do not write questions manually. They continue to map their course topics; the Worker prepares questions from eligible material posted to those topics.
- `/class_poll` remains a separate regular poll feature.

## Course and topic scope

- Store a current semester label on `academic_groups` and add a `course_offerings` table keyed by group, course, and semester label. The existing `courses` row remains the stable course identity; semester offerings track which courses are taught in each term without duplicating course identities.
- Tag each ingested material with the group's current semester label. A course is eligible only when it has an active offering for that exact semester, is active, its group is active, and it is mapped to a topic in that group's chat. Only materials tagged with the same semester are used, so prior-term files cannot silently seed current quizzes.
- A group admin sets the current semester with `/semester_set <label>` (trimmed, 1–40 characters). Course mapping creates or activates that course's offering for the current semester. Changing the current semester does not delete previous offerings or materials.
- A group admin registers the destination by running `/quiz_topic` inside the forum topic named `Quiz`. Store the selected thread ID per group. Do not guess the topic from partial names and do not fall back to General; skip delivery when no Quiz topic is configured.
- Add the bot as a group admin so Telegram delivers course material messages and the Worker can reliably verify group-admin actions; grant the minimum send-message and send-poll permissions required.

## Material ingestion and extraction

- Treat PDFs and common images posted to a mapped course topic as study materials, together with their captions. Also accept text notes when a rep sends `/quiz_source` as a reply to the note in that course topic. Since this command is a bot-directed command update, the bot can associate the replied-to text with the source topic. Exclude ordinary discussion, unrelated commands, links without readable content, and files from other topics or colleges.
- Deduplicate each source by `(chat_id, source_message_id)`. Keep Telegram's `file_id`, source message link, course/topic/semester identity, MIME type, caption, extraction state, and source checksum in D1. Do not retain the downloaded binary after extraction.
- The Worker calls Telegram `getFile` and downloads supported files up to Telegram's 20 MB Bot API download limit. It converts PDF/image bytes to Markdown using the Cloudflare Workers AI `AI.toMarkdown` binding, then stores bounded extracted text in D1 tied to the originating course and semester. Cloudflare documents PDF and common image support; image conversion uses AI models for object detection and description, so this is best-effort extraction rather than guaranteed verbatim OCR. Low-text conversions, such as many scans or handwriting samples, are rejected and reported as needing a clearer or text-based source. [Cloudflare supported formats](https://developers.cloudflare.com/workers-ai/features/markdown-conversion/supported-formats/), [conversion behavior](https://developers.cloudflare.com/workers-ai/features/markdown-conversion/how-it-works/), [Telegram `getFile`](https://core.telegram.org/bots/api#getfile).
- Process extraction through a durable D1 outbox with bounded batches, leases, retry counts, and sanitized errors. Generation must not block webhook acknowledgement or run synchronously for every attachment.
- Store extracted text and source metadata only after validation. Cap extracted text at 50,000 characters per file, require at least 1,500 usable characters across the selected course's current-semester sources, and send at most 18,000 selected source characters to question generation per run. Use college, chat, course, semester, and thread joins in all source queries.

## Generation and delivery schedule

- Use the Cloudflare AI binding for document conversion and structured question generation. Do not require a separate model-provider secret for this path.
- The every-other-day 10:00 Africa/Addis_Ababa job selects the eligible current-semester course least recently quizzed, skipping courses without at least 1,500 usable extracted characters. Generate five questions from that course only.
- Keep the legacy 16:00 Africa/Addis_Ababa day-before-exam review job. Generate up to 20 questions using only the exam course's current-semester materials from its mapped topic. Rate-limit poll sends per group and persist each poll delivery in a D1 outbox so retries cannot replay a completed update.
- Store the generated question set and per-question send state before delivery. Validate each question has a non-empty prompt, four distinct options, one valid correct index, and a source excerpt reference. If generation fails validation or source text is inadequate, do not publish a partial or ungrounded quiz; record a retryable failure and move to another eligible course on a later run.
- Tag each generation run with group, semester, course, scheduled window, and source IDs. Enforce one run per course/schedule window and retain enough history to prevent duplicate quizzes after Cron or webhook retries.
- Do not send raw source text or document attachments to the Quiz topic. Post a short course/date header, then Telegram Quiz polls.

## Free-tier constraints and error handling

- Workers AI currently includes 10,000 Neurons per day on Free; usage past that free allocation fails on Workers Free. Image conversion consumes AI inference for some files, so use a bounded daily extraction budget and expose quota/failure state to operators. [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/).
- The Worker caps this deployment at 25 extraction calls and 10 quiz-generation calls per UTC day in D1. This bounds this Worker’s activity; it cannot reserve or account for Workers AI usage by other Workers on the same Cloudflare account.
- Telegram bot downloads are limited to 20 MB per file. Reject larger files without attempting a download and tell the group admin/reps why they were skipped. [Telegram Bot API `getFile`](https://core.telegram.org/bots/api#getfile).
- Workers Free Cron CPU is 10 ms per invocation. Keep per-run SQL, file extraction, AI calls, and Telegram sends bounded; move remaining work to later Cron runs. [Workers limits](https://developers.cloudflare.com/workers/platform/limits/).
- If a poll send has an ambiguous network result, do not automatically resend that same question: Telegram `sendPoll` has no caller-provided idempotency key. Mark the send as uncertain for operator review to avoid duplicates.
- If no destination Quiz topic is registered, no source material is available, a file is unsupported/oversized, or AI quota is exhausted, skip publishing and record a non-sensitive operational reason. Do not publish to General as a fallback.

## Data and privacy

- Uploaded material is processed by Cloudflare Workers and Workers AI. The app persists extracted text and source metadata in D1, but not the original file bytes.
- Quiz polls are anonymous Telegram Quiz polls. Telegram owns vote state; the Worker does not store voter identities or individual votes. Correct answers and aggregate poll results are visible in the group through Telegram's quiz behavior.
- Keep generation runs, course semester, material extraction state, and poll outbox records college-scoped.

## Checkpoint acceptance

1. A group admin sets the current semester and registers the destination topic from the topic named `Quiz`.
2. Only active courses from the current semester and their mapped topics are considered.
3. New PDFs and common image formats posted in course topics are extracted into bounded text; duplicate messages are extracted once; ordinary discussion and cross-topic materials are excluded.
4. Generated polls use only the selected course's extracted sources, have four options and one valid answer, and post directly into the configured Quiz topic.
5. A run with missing materials, a missing destination topic, or an AI failure sends no partial quiz and does not fall back to General.
6. Repeated Cron execution does not duplicate a scheduled quiz or confirmed poll; uncertain sends are surfaced rather than replayed automatically.
7. Tests cover semester filtering, course/topic/college isolation, PDF/image conversion failures, AI output validation, schedule selection, send rate limits, and duplicate suppression.
8. Run a live test in a dedicated authorized test group before changing Telegram's production webhook.

## Decisions in this spec

- **Course rotation:** Least-recently-quizzed eligible course, replacing random selection to improve coverage.
- **Text notes:** Reps explicitly mark standalone notes with `/quiz_source`; attached PDFs/images and their captions are ingested automatically.
- **Destination selection:** Admin explicitly registers the Quiz topic with `/quiz_topic`, because the Worker cannot safely infer forum-topic names from its current D1 mappings.
- **Existing private quiz implementation:** Retain its schema but stop creating new private quizzes; new automated course quizzes use Telegram poll tables/outboxes.
