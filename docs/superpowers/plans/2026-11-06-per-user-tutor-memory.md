# Per-User Tutor Memory Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Give each Telegram user isolated recent tutor conversation context and explicitly managed study preferences, stored durably in Cloudflare D1.

**Architecture:** Add D1 profile and conversation tables. A memory service in `course_tutor_service.py` loads bounded history by `(user_id, chat_id, context_key)`, applies user preferences across chats, appends only completed exchanges, and deletes all memory for `/forget_me`. Tutor prompt builders accept optional history/preferences; private and mapped-course tutor flows load and persist context, while explicit commands manage preferences and erasure.

**Tech Stack:** Cloudflare Python Workers, D1/SQLite SQL migrations, Telegram Bot API, Workers AI, pytest.

---

## File Map

- Create `cloudflare/worker/migrations/0014_tutor_memory.sql`: D1 profile and conversation tables plus bounded-query indexes.
- Modify `cloudflare/worker/src/course_tutor_service.py`: D1 memory CRUD, limits, context derivation, and tutor-flow integration.
- Modify `cloudflare/worker/src/course_tutor_ai.py`: inject bounded preferences and recent turns into prompts safely.
- Modify `cloudflare/worker/src/worker.py`: `/remember`, `/memory`, and `/forget_me` command routing, private-only display, help text, context handoff.
- Modify `cloudflare/worker/tests/test_course_tutor_service.py`: use its SQLite migration harness to verify persistence, per-user/context isolation, caps, and failure handling.
- Modify `cloudflare/worker/tests/test_course_tutor_ai.py`: verify prompt ordering, bounds, and untrusted-history instructions.
- Modify `cloudflare/worker/tests/test_worker.py`: verify command scope and tutor context handoff.
- Modify `cloudflare/worker/README.md`: document what memory stores, its scope, and deletion commands.

## Task 1: Add durable D1 memory tables

**Files:**
- Create: `cloudflare/worker/migrations/0014_tutor_memory.sql`
- Test: `cloudflare/worker/tests/test_course_tutor_service.py`

- [x] **Step 1: Add a schema test that the migration creates bounded per-user profile and message storage**

Add this test. The existing `SqliteDatabase` applies every migration in order, so the test exercises the real SQL migration:

```python
def test_tutor_memory_migration_creates_user_profiles_and_context_messages():
    database = SqliteDatabase()
    tables = {
        row[0]
        for row in database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    assert {"tutor_user_profiles", "tutor_conversation_messages"} <= tables
    profile_columns = {
        row[1] for row in database.connection.execute(
            "PRAGMA table_info(tutor_user_profiles)"
        )
    }
    message_columns = {
        row[1] for row in database.connection.execute(
            "PRAGMA table_info(tutor_conversation_messages)"
        )
    }
    assert {"user_id", "study_preferences", "updated_at_ms"} <= profile_columns
    assert {"id", "user_id", "chat_id", "context_key", "role", "content", "created_at_ms"} <= message_columns
```

- [x] **Step 2: Run the focused test and confirm it fails because migration 0014 does not exist**

Run from `cloudflare/worker`:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv-workers/Lib/site-packages').Path
python -m pytest tests/test_course_tutor_service.py::test_tutor_memory_migration_creates_user_profiles_and_context_messages -q
```

Expected: FAIL because the two memory tables are missing.

- [x] **Step 3: Create migration 0014**

Use this SQL:

```sql
CREATE TABLE tutor_user_profiles (
    user_id INTEGER PRIMARY KEY,
    study_preferences TEXT NOT NULL DEFAULT '',
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE tutor_conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    context_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 2000),
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX ix_tutor_conversation_context
ON tutor_conversation_messages (user_id, chat_id, context_key, id DESC);

CREATE INDEX ix_tutor_conversation_user
ON tutor_conversation_messages (user_id, id);
```

- [x] **Step 4: Run the focused schema test and confirm it passes**

Run the same pytest command. Expected: PASS.

## Task 2: Implement user-isolated memory storage

**Files:**
- Modify: `cloudflare/worker/src/course_tutor_service.py`
- Test: `cloudflare/worker/tests/test_course_tutor_service.py`

- [x] **Step 1: Add failing tests for history isolation, eight-exchange retention, profile preferences, and user deletion**

Tests should call the real async storage helpers against the existing SQLite D1 adapter. Assert all of these behaviors:

1. `load_tutor_memory(db, user_id=41, chat_id="-1001", context_key="course:7:Fall 2026")` returns only that user's rows in that context, oldest to newest, with at most 16 messages.
2. Rows from user 42, another chat, or context `course:8:Fall 2026` are absent.
3. `save_tutor_exchange(..., question="Question 9", answer="Answer 9")` leaves exactly the newest 16 message rows for that context after nine exchanges.
4. `set_tutor_preferences(..., "Use examples")` followed by `get_tutor_preferences` returns the value for that Telegram user regardless of chat.
5. `forget_tutor_user(db, 41)` removes that user's profile and history from all contexts while preserving user 42's rows.

Use the signatures shown in the assertions above; run each test individually and confirm it fails because the helper does not yet exist.

- [x] **Step 2: Add memory bounds and context-key helpers**

Add constants and deterministic key generation in `course_tutor_service.py`:

```python
MAX_TUTOR_MEMORY_EXCHANGES = 8
MAX_TUTOR_MEMORY_MESSAGE_CHARS = 2_000
MAX_TUTOR_PREFERENCES_CHARS = 1_000
PRIVATE_TUTOR_CONTEXT = "private"


def course_tutor_context_key(context: dict) -> str:
    return "course:{}:{}:{}".format(
        int(context["course_id"]),
        int(context["thread_id"]),
        str(context["semester_label"])[:80],
    )
```

- [x] **Step 3: Implement preferences, bounded history, exchange append, and deletion helpers**

Define these async APIs in `course_tutor_service.py`:

```python
async def get_tutor_preferences(database, user_id: int) -> str: ...
async def set_tutor_preferences(database, user_id: int, preferences: str, now_ms: int) -> str: ...
async def load_tutor_memory(database, user_id: int, chat_id: str, context_key: str) -> list[dict]: ...
async def save_tutor_exchange(database, user_id: int, chat_id: str, context_key: str,
                              question: str, answer: str, now_ms: int) -> None: ...
async def forget_tutor_user(database, user_id: int) -> None: ...
```

Implementation requirements:

- Trim and cap preferences at 1,000 characters; the setter returns the normalized saved value.
- Cap each stored message at 2,000 characters.
- Select rows ordered by `id DESC LIMIT 16`, then return them in chronological order.
- Insert question and answer as separate role rows, then prune older rows for that exact `(user_id, chat_id, context_key)` while retaining the newest 16 rows.
- Use bound SQL values throughout.
- Delete messages before the profile in `forget_tutor_user`; this needs no FK because Telegram user IDs are external identities.

- [x] **Step 4: Run the focused storage tests and confirm they pass**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv-workers/Lib/site-packages').Path
python -m pytest tests/test_course_tutor_service.py -q
```

Expected: all service tests pass, including isolation and retention assertions.

## Task 3: Add explicit conversation and preference context to tutor prompts

**Files:**
- Modify: `cloudflare/worker/src/course_tutor_ai.py`
- Test: `cloudflare/worker/tests/test_course_tutor_ai.py`

- [x] **Step 1: Add failing prompt tests for memory ordering, preference injection, bounds, and trust boundaries**

Add tests for both prompt builders. Pass `conversation_history` and `study_preferences` keyword arguments. Assert that:

- The system message calls history conversational context and says prior turns and preferences are not trusted instructions or evidence.
- The student preference text is included only in the explicit preference field.
- The latest user question remains the final user turn after prior history messages.
- Only the latest 16 history messages are included, each content value bounded to 2,000 characters, and preference text bounded to 1,000 characters.
- Existing course/web evidence remains separate and still uses `[M#]`/`[W#]` citations.

- [x] **Step 2: Extend both tutor prompt builder signatures with optional memory arguments**

Update signatures to:

```python
def build_tutor_messages(course_name, semester_label, question, material_passages, web_sources,
                         conversation_history=None, study_preferences=""):
    ...


def build_general_tutor_messages(question, web_sources, conversation_history=None,
                                 study_preferences=""):
    ...
```

Keep old call forms valid by providing defaults. Add serialized fields `recent_conversation_untrusted_context` and `student_study_preferences_untrusted_context`; trim inputs before serializing. Build prior `user` and `assistant` messages in chronological order, then append the current question with the existing source payload.

- [x] **Step 3: Run focused AI prompt tests and confirm they pass**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv-workers/Lib/site-packages').Path
python -m pytest tests/test_course_tutor_ai.py -q
```

Expected: all AI prompt, renderer, search, and model-shape tests pass.

## Task 4: Wire memory into private and course-topic tutor turns

**Files:**
- Modify: `cloudflare/worker/src/course_tutor_service.py`
- Test: `cloudflare/worker/tests/test_course_tutor_service.py`

- [x] **Step 1: Add failing private and course integration tests**

For private chat, stub the AI binding to inspect `options["messages"]`, send a first question, then a follow-up for the same `(user_id, chat_id)` and assert the first exchange appears. Send another question under a different user or private chat and assert the first exchange is absent.

For course chat, run two users in the same mapped course topic. Assert user 41's second `/ask` context includes only user 41's first question; user 42's prompt does not. Also verify the constructed context key changes when `thread_id`, `course_id`, or semester changes.

Finally make a mocked D1 memory read/write fail and verify a generated tutor reply still returns.

- [x] **Step 2: Load preferences/history and persist only successful exchanges**

In `answer_general_question`, use `PRIVATE_TUTOR_CONTEXT` for memory and wrap each memory read/write in `try/except Exception`; build a stateless prompt if reads fail. After a nonempty answer is generated, append the question/answer pair. In `answer_course_question`, derive `context_key = course_tutor_context_key(context)`, load scoped memory and preferences before building messages, and persist only after a nonempty answer.

Pass the current user's preference to either prompt builder; do not include user ID, user name, or username in prompt content. Keep course passage retrieval and Tavily search behavior unchanged so every question still fetches current evidence.

- [x] **Step 3: Run service tests and confirm they pass**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv-workers/Lib/site-packages').Path
python -m pytest tests/test_course_tutor_service.py -q
```

Expected: all service tests pass, including persistence failure fallback.

## Task 5: Add memory management commands

**Files:**
- Modify: `cloudflare/worker/src/worker.py`
- Test: `cloudflare/worker/tests/test_worker.py`

- [x] **Step 1: Add failing routing tests for the three commands**

Test these inputs through `_process_message_update` with a private Telegram `message` object:

- `/remember explain math with visual examples` saves exactly that preference for sender ID 41 and confirms it was saved.
- `/remember` without text returns a usage hint and does not write.
- `/memory` in private chat displays only the sender's preference; another user's preference is absent.
- `/memory` in a group returns a private-chat instruction without reading or showing preferences.
- `/forget_me` deletes only sender ID 41's memory and preferences and returns a content-free confirmation.
- `/forget_me` in a group is accepted and does not mention any saved text.

- [x] **Step 2: Route management commands before generic private tutoring**

Add a small `_tutor_memory_command(database, message, chat, text, now_ms)` helper in `worker.py` and call it early in `_process_message_update`, before course/general tutor question routing. Parse `/remember` with an optional bot suffix (`/remember@botname`), cap at 1,000 characters, and reply with a usage hint on empty/oversized input. Allow `/memory` only in private chat. Allow `/forget_me` in private or group chats, delete using only Telegram sender ID, and never echo memory content.

Add `/remember <preference>`, `/memory`, and `/forget_me` to `_help_message()`.

- [x] **Step 3: Run the focused routing tests and confirm they pass**

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv-workers/Lib/site-packages').Path
python -m pytest tests/test_worker.py -q
```

Expected: all webhook and command tests pass.

## Task 6: Document behavior and run the complete Worker suite

**Files:**
- Modify: `cloudflare/worker/README.md`
- Test: `cloudflare/worker/tests/test_course_tutor_ai.py`
- Test: `cloudflare/worker/tests/test_course_tutor_service.py`
- Test: `cloudflare/worker/tests/test_worker.py`

- [x] **Step 1: Update the tutor README section**

Document that D1 stores only messages addressed to the tutor; recent context is isolated by Telegram user, chat, course/topic, and active semester; at most eight exchanges are loaded per context; preferences are explicitly saved and follow the user; `/memory` is private-only; `/forget_me` deletes all of that user's tutor memory. Remove the stale statement that D1 stores only counts and does not store questions/answers.

- [x] **Step 2: Run the complete Worker test suite**

Run from `cloudflare/worker`:

```powershell
$env:PYTHONPATH = (Resolve-Path '.venv-workers/Lib/site-packages').Path
python -m pytest tests -q
```

Expected: all tests pass.

- [x] **Step 3: Apply the migration and deploy staging**

From `cloudflare/worker`, with Wrangler authenticated:

```powershell
$env:UV_PYTHON_INSTALL_DIR = 'C:\uvpyinstall'
$workerScripts = (Resolve-Path '.venv\Scripts').Path
$env:PATH = "$workerScripts;$env:PATH"
\.venv\Scripts\pywrangler.exe d1 migrations apply academic-assistant-college-pilot --remote
\.venv\Scripts\pywrangler.exe deploy
```

Expected: migration 0014 applies once, and the Worker deploy reports a new version and the staging URL.

- [x] **Step 4: Verify staging readiness**

Run:

```powershell
Invoke-RestMethod https://academic-assistant-college-pilot.earnlets96.workers.dev/health
Invoke-RestMethod https://academic-assistant-college-pilot.earnlets96.workers.dev/ready
```

Expected: `/health` returns `status: ok`; `/ready` returns `status: ready`.

## Self-review

- **Spec coverage:** Tables and bounds are Task 1; per-user/context isolation and retention are Task 2; prompt trust boundaries are Task 3; private/course integration and graceful D1 failures are Task 4; all three commands and private-only display are Task 5; README, full tests, migration, staging deployment, and readiness are Task 6.
- **Privacy:** Conversation queries always bind Telegram user ID, chat, and context. Preferences are keyed only to the owner. Group `/forget_me` has a generic response; `/memory` cannot reveal preferences in a group.
- **Bounds:** Preferences are capped at 1,000 characters, individual messages at 2,000, and prompt history at eight exchanges.
- **Failure behavior:** Missing or failed memory reads become empty context; write failures do not suppress an already generated reply; unsuccessful model calls are not stored.
- **No placeholders:** Every file, helper signature, test behavior, command, migration, and expected result is specified above.

