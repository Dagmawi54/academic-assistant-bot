# Class Polls Implementation Plan

**Goal:** Let registered class reps and group admins post simple anonymous polls in a class group or its current forum topic.

**Design:** Add a strict `/class_poll Question | Option A | Option B [| ...]` parser. Require a registered group and current admin/rep authorization, then post a Telegram regular poll to the originating group/topic. Telegram handles votes; the Worker does not persist voter identities or answers.

**Checks:** Parser tests, Worker authorization/routing tests, Telegram transport payload test, idempotent receipt test, Ruff, Worker suite, Wrangler dry run, staging migration/deployment, and hosted health check. Keep the production webhook unset.

**Out of scope:** Poll editing/closing, multiple-select polls, surveys spanning groups, and stored vote analytics.

## Progress

- [x] Implement `/class_poll` validation for 2–10 distinct options.
- [x] Enforce registered-group admin/rep permission and post to the source chat/topic.
- [x] Use anonymous Telegram single-choice polls; keep voter data out of D1.
- [x] Add durable update receipts to suppress duplicate poll sends.
- [x] Run focused Worker checks: 56 tests pass, Ruff passes, and Wrangler dry run succeeds.
- [x] Apply migration `0010`, deploy the Worker, and verify hosted `/health`, `/ready`, and no pending D1 migrations.
- [ ] Complete live poll test in a dedicated class test group.
