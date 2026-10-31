"""Gate unit tests: the validation checks are exercised by a test —
no network, deterministic inputs. Also covers distribution enforcement (check 7).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gen


def good_question(stem="Which planet is known as the Red Planet?",
                  answer="Mars", options=("Venus", "Jupiter", "Saturn", "Mars"),
                  confidence="high"):
    return {"stem": stem, "options": list(options), "answer": answer,
            "confidence": confidence, "category": "science"}


class GateTest(unittest.TestCase):
    def _validate(self, q, pool=()):
        return gen._validate_question(q, set(pool))

    def test_good_question_passes(self):
        ok, reason = self._validate(good_question())
        self.assertTrue(ok, reason)

    def test_schema_missing_field(self):
        q = good_question(); del q["confidence"]
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("schema", reason)

    def test_choice_count_not_four(self):
        q = good_question(options=("Mars", "Venus", "Jupiter"))
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("choices:count", reason)

    def test_duplicate_choices(self):
        q = good_question(options=("Mars", "Mars", "Venus", "Jupiter"))
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("duplicate", reason)

    def test_answer_not_in_choices(self):
        q = good_question(answer="Earth")
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("answer:not_in_choices", reason)

    def test_answer_leak_in_stem(self):
        q = good_question(stem="The planet Mars is known as the Red Planet.", answer="Mars")
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("leak", reason)

    def test_stem_too_short(self):
        q = good_question(stem="What planet?", answer="Mars")
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("length", reason)

    def test_low_confidence_rejected(self):
        q = good_question(confidence="low")
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("confidence:low", reason)

    def test_invalid_confidence_rejected(self):
        q = good_question(confidence="maybe")
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("confidence:invalid", reason)

    def test_volatile_stem_without_year_rejected(self):
        q = good_question(stem="Who is the current record holder for the most Olympic gold medals?")
        ok, reason = self._validate(q)
        self.assertFalse(ok); self.assertIn("volatile", reason)

    def test_volatile_stem_with_year_passes(self):
        q = good_question(stem="As of 2024, who holds the record for the most Olympic gold medals?",
                          answer="Mars")
        ok, reason = self._validate(q)
        self.assertTrue(ok, reason)

    def test_near_duplicate_rejected(self):
        q1 = good_question(stem="Which planet is known as the Red Planet and is fourth from the sun?")
        key1 = gen._stem_key(q1["stem"])
        q2 = good_question(stem="Which planet is known as the Red Planet and is fourth from the sun?")
        ok, reason = self._validate(q2, pool={key1})
        self.assertFalse(ok); self.assertIn("duplicate", reason)

    def test_near_duplicate_word_jaccard_accepts_unrelated(self):
        # Two unrelated questions sharing only "which"/"the" must not be
        # near-duplicates (the old character-set gate rejected these).
        river = "Which river holds the title of the longest flowing waterway in South America?"
        mountain = ("Which mountain range forms the traditional continental divide "
                    "between Europe and Asia?")
        pool = {gen._stem_key(river)}
        ok, reason = self._validate(good_question(stem=mountain), pool=pool)
        self.assertTrue(ok, reason)

    def test_near_duplicate_word_jaccard_rejects_reworded_duplicate(self):
        # A genuinely reworded duplicate shares nearly all of its words and
        # must still be rejected by word-token Jaccard >= 0.85.
        orig = "Which country has the longest coastline of any nation in the world?"
        reworded = "Which nation has the longest coastline of any country in the world?"
        pool = {gen._stem_key(orig)}
        ok, reason = self._validate(good_question(stem=reworded), pool=pool)
        self.assertFalse(ok)
        self.assertEqual(reason, "duplicate:near")

    def test_distribution_enforced(self):
        # 9 questions, all correct answer originally in position 0.
        qs = [good_question(answer="Mars", options=("Mars", "Venus", "Jupiter", "Saturn"))
              for _ in range(9)]
        out = gen._enforce_distribution(qs)
        hist = {}
        for q in out:
            idx = [gen._normalize(o) for o in q["options"]].index(gen._normalize(q["answer"]))
            hist[idx] = hist.get(idx, 0) + 1
        self.assertTrue(gen._distribution_ok(hist, len(out)))
        # Answer text must still be present exactly once after reordering.
        for q in out:
            self.assertEqual(sum(1 for o in q["options"] if gen._normalize(o) == gen._normalize(q["answer"])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
