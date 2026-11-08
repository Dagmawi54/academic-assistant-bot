# Per-User Tutor Memory Design

## Goal

Let the Telegram academic assistant continue a student's recent tutor conversation and apply study preferences the student explicitly saves. Memory must remain separated by Telegram user and conversation context, including when multiple students use the same course topic.

## Approved approach

Use Cloudflare D1 for durable tutor memory. Store only tutor interactions, not ordinary group messages. Keep recent conversation history for each `(user_id, chat_id, context_key)` and keep study preferences under the Telegram `user_id` so they can follow the student across private and group chats.

The tutor will use the latest eight user/assistant exchanges for the matching context. A private chat has its own context. A mapped course topic uses a context key that includes the group, topic, course, and active semester, so context cannot spill into another topic or term. Each student's group-topic history is isolated by their Telegram user ID.

The bot will not infer or automatically save personal facts from ordinary conversation. A student manages study preferences explicitly with:

- `/remember <preference>` to save a preference, such as `/remember use examples when explaining math`.
- `/memory` to view the saved study preferences and confirm that conversation memory is enabled.
- `/forget_me` to remove that student's saved preferences and tutor conversation history across all chats and topics.

Memory-management commands that display saved information will work in private chat only. `/forget_me` may be used in private chat or a group/topic and will not reveal the deleted content in its confirmation.

## Data model

Add a D1 migration for two tables:

- A profile table keyed by Telegram `user_id`, with bounded preference text and an update timestamp.
- A conversation-memory table with a generated ID, `user_id`, `chat_id`, `context_key`, role (`user` or `assistant`), bounded message content, and creation time. Add an index ordered by user, chat, context, and creation time for bounded history retrieval and cleanup.

Each successful tutor answer appends the user's question and the assistant reply. Keep only the newest eight complete exchanges per user and context. Never store a failed model request as a completed exchange. `/forget_me` deletes rows from both tables for exactly the requesting Telegram user.

## Tutor behavior

Load only the requesting user's recent messages for the current private chat or mapped course-topic context. Add them as prior conversation context to the tutor prompt. Treat prior user text as untrusted instructions and prior assistant replies as conversational context, not as factual evidence. Continue to retrieve course passages and current web sources for each new question rather than treating old answers as authoritative.

Pass the student's saved preferences to the tutor prompt for both private and course-topic questions. Keep history and preferences within strict size bounds. If memory reads fail, answer without memory; if memory writes fail after an answer is generated, deliver the answer and log a bounded storage error without recording message content in logs.

## Privacy and scope

- Key all conversation reads and writes by Telegram `user_id`, chat, and context. Never load another student's messages from a shared group or topic.
- Store only messages sent to the tutor, not ambient Telegram messages.
- Do not add student names, usernames, or Telegram IDs to model prompts for memory purposes.
- Keep preferences explicit and user-editable; do not automatically infer sensitive personal details.
- `/forget_me` removes all tutor memory for that Telegram user across private chat and group contexts.
- Update bot help and operations documentation to explain what is stored and how to clear it.

## Failure behavior

Memory is an enhancement, not a prerequisite for tutoring. Missing rows mean an empty memory. A retrieval error falls back to a stateless answer. A persistence error does not suppress a generated answer. Preference command validation rejects empty or oversized values with a short usage hint. D1 remains the durable source of truth; no local database or in-memory-only persistence is introduced.

## Verification criteria

- A user's follow-up question receives only that user's prior messages from the same chat/topic/semester context.
- Two students sharing one topic cannot retrieve each other's conversation history or preferences.
- Private-chat history, one group topic, a second topic, and a new semester remain isolated.
- The context sent to the model includes at most eight exchanges and bounded preferences.
- `/remember`, `/memory`, and `/forget_me` enforce their scope and size rules.
- `/forget_me` deletes only the invoking user's memories and preferences.
- Failed AI calls do not save incomplete conversations; D1 failures do not prevent delivery of generated answers.
- Existing tutor, citation, and Telegram formatting behavior remains intact.
