import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import batch_exercises as be  # noqa: E402
import course_tutor_ai  # noqa: E402

SHEET = "Exercises 1 - Control Statements " + " ".join(
    f"{n}. Write a program in Java to do task number {n} of the sheet." for n in range(1, 13)
)


class Stmt:
    def __init__(self, db, q):
        self.db, self.q, self.v = db, q, ()

    def bind(self, *v):
        self.v = v
        return self

    async def first(self):
        r = self.db.c.execute(self.q, self.v).fetchone()
        return dict(r) if r else None

    async def run(self):
        self.db.c.execute(self.q, self.v)
        self.db.c.commit()


class Db:
    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        migrations = Path(__file__).parents[1] / "migrations"
        self.c.executescript((migrations / "0018_tutor_documents.sql").read_text())
        self.c.executescript((migrations / "0019_followup_sets.sql").read_text())

    def prepare(self, q):
        return Stmt(self, q)


class FakeAi:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    async def run(self, model, payload):
        task = payload["messages"][-1]["content"]
        self.calls.append(task)
        if any(f"Exercise {n} of" in task for n in self.fail):
            raise RuntimeError("boom")
        return {"response": "Loop idea.\n\n```java\nclass A { }\n```\n\nSample run:\n```text\n1\n```"}


def split(text, limit):
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


def run(coro):
    return asyncio.run(coro)


class ParseTests(unittest.TestCase):
    def test_parses_twelve_questions_and_language(self):
        q = be.parse_exercises(SHEET)
        self.assertEqual([n for n, _ in q], list(range(1, 13)))
        self.assertEqual(be.detect_language(SHEET), "Java")

    def test_plain_prose_is_not_an_exercise_sheet(self):
        self.assertEqual(be.parse_exercises("Neuroplasticity is the brain's ability to adapt. It has 2 parts."), [])

    def test_workbook_detection_uses_exercise_cues_not_just_numbered_sections(self):
        self.assertTrue(be.looks_like_exercise_sheet("Question 1: Write a program. 2. Calculate x. 3. Solve y."))
        self.assertFalse(be.looks_like_exercise_sheet("1. Introduction 2. Background 3. Conclusion"))

    def test_intents(self):
        self.assertTrue(be.wants_solve_all("do them"))
        self.assertTrue(be.wants_continue("next"))
        self.assertEqual(be.question_selection("solve q3 and q7", 12), [3, 7])
        self.assertEqual(be.question_selection("do questions 4 to 6", 12), [4, 5, 6])
        self.assertFalse(be.wants_solve_all("what is a loop"))
        self.assertTrue(be.wants_summary("summarise this"))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.db = Db()
        q = be.parse_exercises(SHEET)
        run(be.save_document(self.db, 7, "7", "sheet.pdf", SHEET, q, "Java", 1000))
        self.doc = run(be.load_document(self.db, 7, "7", 2000))

    def batch(self, ai, numbers=None, lesson=True):
        sent = []

        async def send(text, markup):
            sent.append((text, markup))
            return len(sent)

        cleared = self.cleared = []

        async def clear(mid):
            cleared.append(mid)
        send.clear = clear

        async def make(title, text):
            return "https://t.me/iv?url=x"

        run(be.run_batch(
            database=self.db, ai_binding=ai, send=send, split=split, user_id=7, chat_id="7",
            document=self.doc, numbers=numbers or list(range(1, 13)), make_lesson=make if lesson else None,
        ))
        return sent

    def test_every_question_gets_its_own_labelled_message_and_buttons_only_at_the_end(self):
        sent = self.batch(FakeAi())
        labels = [t for t, _ in sent if "Question" in t and "of 12" in t and t.startswith("📘")]
        self.assertEqual(len(labels), 12)
        for n in range(1, 13):
            self.assertTrue(any(f"Question {n} of 12" in t for t, _ in sent))
        early = [m for _, m in sent[:-1] if m]
        self.assertTrue(all("ex:c:0" in str(m) for m in early))  # only Continue, and it is removed later
        self.assertEqual(len(early), len(self.cleared))
        keyboard = sent[-1][1]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "📖 Open visual lesson")
        self.assertEqual(sum(1 for row in keyboard for b in row if b.get("callback_data", "").startswith("ex:e:")), 12)
        self.assertTrue(all(course_tutor_ai.is_valid_telegram_html(t) for t, _ in sent))

    def test_continue_button_only_on_newest_group_bubble_and_cleared_afterwards(self):
        sent = self.batch(FakeAi())
        with_buttons = [i for i, (_, m) in enumerate(sent) if m]
        self.assertEqual(with_buttons, [4, 8, 13])  # group 1, group 2 (Continue) and the closing bubble
        self.assertEqual(self.cleared, [5, 9])  # message ids of the two Continue bubbles are cleared

    def test_cut_off_job_leaves_continue_on_last_sent_bubble(self):
        class Boom(FakeAi):
            async def run(self, model, payload):
                if any(f"Exercise {n} of" in payload["messages"][-1]["content"] for n in (5, 6, 7, 8)):
                    raise KeyboardInterrupt  # simulates the worker being killed mid-job
                return await super().run(model, payload)
        with self.assertRaises(BaseException):
            self.batch(Boom())
        # group 1 delivered with a Continue button for Q5-Q12 on its last bubble
        doc = run(be.load_document(self.db, 7, "7", 2000))
        self.assertEqual(doc["done"], [1, 2, 3, 4])

    def test_failed_question_is_reported_and_retry_button_appears(self):
        sent = self.batch(FakeAi(fail={4}))
        self.assertTrue(any("Question 4 of 12" in t and "could not" in t for t, _ in sent))
        self.assertIn("ex:r:0", str(sent[-1][1]))
        done = run(be.load_document(self.db, 7, "7", 2000))["done"]
        self.assertNotIn(4, done)
        self.assertEqual(len(done), 11)

    def test_long_answer_continues_in_a_new_message_with_the_question_label(self):
        parts = be.render_question(3, 12, "Q text", "word " * 2000, split)
        self.assertGreater(len(parts), 1)
        self.assertIn("Question 3 of 12", parts[1])

    def test_followup_explain_sends_new_messages(self):
        sent = []

        async def send(text, markup):
            sent.append((text, markup))

        run(be.run_followup(database=self.db, ai_binding=FakeAi(), send=send, split=split, user_id=7,
                            chat_id="7", document=self.doc, action="e", number=5))
        self.assertIn("Question 5 of 12", sent[0][0])
        self.assertIsNotNone(sent[-1][1])

    def test_document_expires(self):
        self.assertIsNone(run(be.load_document(self.db, 7, "7", 1000 + be.DOCUMENT_TTL_MS + 1)))


if __name__ == "__main__":
    unittest.main()


import followups  # noqa: E402
followups.course_tutor_ai = course_tutor_ai  # The tutor quality loader stubs sibling modules.


class FollowupTests(unittest.TestCase):
    def test_parse_and_keyboard_are_built_from_answer_context(self):
        raw = "1. Why use sqrt(n) | Why only check up to the square root of the number?\n- Try n = 49 | Can you trace the loop for n = 49?\nbad line\nCommon mistake | What mistake do beginners make with isPrime?"
        items = followups.parse_suggestions(raw)
        self.assertEqual(len(items), 3)
        kb = followups.keyboard(items, None, truncated=False, key="a1b2")
        self.assertEqual([r[0]["callback_data"] for r in kb["inline_keyboard"]], ["fu:s:a1b2:0", "fu:s:a1b2:1", "fu:s:a1b2:2"])
        self.assertTrue(all(len(r[0]["text"]) <= 24 for r in kb["inline_keyboard"]))
        self.assertNotIn("Continue", str(kb))

    def test_followup_button_labels_keep_the_topic_and_avoid_generic_actions(self):
        items = followups.parse_suggestions(
            "Why | Why does the universe keep expanding?\n"
            "Check | What evidence supports the Big Bang?"
        )
        self.assertEqual(len(items), 2)
        self.assertIn("universe", items[0]["label"].casefold())
        self.assertIn("big", items[1]["label"].casefold())
        self.assertIn("bang", items[1]["label"].casefold())
        self.assertNotIn(items[0]["label"], {"Why?", "Check"})
        self.assertNotIn(items[1]["label"], {"Why?", "Check"})
        self.assertLessEqual(len(f"💬 {items[0]['label']}"), 24)
        self.assertLessEqual(len(f"💬 {items[1]['label']}"), 24)

    def test_continue_only_when_truncated(self):
        self.assertIn("fu:c:0", str(followups.keyboard([], None, truncated=True)))
        self.assertIsNone(followups.keyboard([], None, truncated=False))

    def test_suggestions_do_not_repeat_button_actions(self):
        items = followups.parse_suggestions(
            "Check | What is the relationship between distance and Hubble's Law?\n"
            "Quick check | Can you check Hubble's Law for a galaxy 100 Mpc away?\n"
            "Example | Can you work through Hubble's Law with a different distance?"
        )

        filtered = followups._relevant_suggestions(
            items,
            "Explain Hubble's Law and recession velocity.",
            "Hubble's Law relates a galaxy's distance to its recession velocity.",
        )

        self.assertEqual(len(filtered), 2)
        self.assertTrue(filtered[0]["label"].startswith("Check:"))
        self.assertTrue(filtered[1]["label"].startswith("Example:"))

    def test_suggest_uses_model_and_roundtrips_storage(self):
        class Ai:
            async def run(self, model, payload):
                return {"response": "Why sqrt | Why stop at the square root?\nTrace 49 | Can you trace the divisor checks for n = 49?\nPitfall | What goes wrong for n = 1?"}
        answer = (
            "<b>Prime check</b> For n = 49, trace possible divisors only up to the square root. "
            "This avoids the common mistake of using a wrong bound; if the loop goes wrong for n = 1, check the edge case. "
        ) + "The loop tests divisibility. " * 4
        items = run(followups.suggest(Ai(), "is 49 prime", answer))
        self.assertEqual(len(items), 3)
        db = Db()
        db = Db()
        run(followups.save(db, 7, "7", items, 10, key="a1b2"))
        run(followups.save(db, 7, "7", [{"label": "Why?", "question": "Why?"}], 11, key="c3d4"))
        self.assertEqual(run(followups.load(db, 7, "7", 20, key="a1b2")), items)
        self.assertEqual(run(followups.load(db, 7, "7", 20, key="c3d4"))[0]["label"], "Why?")
