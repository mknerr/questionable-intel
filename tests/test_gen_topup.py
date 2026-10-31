"""Top-up tests: generation keeps asking until ``count`` questions are
accepted or the call budget is exhausted, and reports the shortfall when it
cannot fill it.

Deterministic — ``gen._chat`` is monkeypatched per test, no network.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gen


def _q(stem, answer, options):
    return {"stem": stem, "options": options, "answer": answer, "confidence": "high"}


RED = _q("Which planet is known as the Red Planet in our solar system?",
         "Mars", ["Venus", "Mars", "Jupiter", "Saturn"])
PHOTO = _q("Which gas do plants absorb from the air for photosynthesis?",
           "Carbon dioxide", ["Oxygen", "Nitrogen", "Carbon dioxide", "Hydrogen"])
SPIDER = _q("How many legs does an adult house spider have?",
            "Eight", ["Six", "Eight", "Ten", "Twelve"])
MERCURY = _q("Which metal is liquid at room temperature and used in thermometers?",
             "Mercury", ["Mercury", "Lead", "Zinc", "Tin"])
FRESH = _q("Which gas makes up the majority of the Earth's atmosphere?",
           "Nitrogen", ["Oxygen", "Nitrogen", "Carbon dioxide", "Argon"])
CLOSEST = _q("Which planet orbits closest to the Sun?",
             "Mercury", ["Earth", "Venus", "Mercury", "Mars"])
GOLD = _q("What is the chemical symbol for the element gold?",
          "Au", ["Ag", "Au", "Fe", "Cu"])


class TopUpTest(unittest.TestCase):
    def setUp(self):
        self._orig_chat = gen._chat
        self.addCleanup(self._restore)

    def _restore(self):
        gen._chat = self._orig_chat

    def _run(self, fake, count=5):
        gen._chat = fake
        return gen.generate_questions("physics", count, model="fake", base="http://fake")

    def test_topup_reaches_requested_count(self):
        calls = []

        def fake(messages, model, base, api_key, timeout, num_predict):
            calls.append(messages)
            n = len(calls)
            if n == 1:
                # 4 unique + 1 exact duplicate.
                return json.dumps([RED, dict(RED), PHOTO, SPIDER, MERCURY])
            if n in (2, 3):
                # Corrective retries keep returning the duplicate, so it is
                # rejected (exercising the retry path, not the top-up).
                return json.dumps([dict(RED)])
            # The top-up call supplies a fresh, distinct question.
            return json.dumps([FRESH])

        res = self._run(fake, count=5)
        self.assertEqual(len(res["accepted"]), 5)
        self.assertEqual(res["requested"], 5)
        self.assertEqual(res["shortfall"], 0)
        self.assertEqual(len(res["rejected"]), 1)
        self.assertGreaterEqual(len(calls), 4, "a top-up call should have run")

    def test_oversized_batch_is_capped(self):
        # N6b regression: a model that returns MORE valid, distinct questions
        # than requested must be trimmed back to `count`, not over-delivered.
        batch = [RED, PHOTO, SPIDER, MERCURY, FRESH, CLOSEST, GOLD]
        gen._chat = lambda *a, **k: json.dumps(batch)
        res = gen.generate_questions("physics", 5, model="fake", base="http://fake")
        self.assertEqual(len(res["accepted"]), 5)
        self.assertEqual(res["requested"], 5)
        self.assertEqual(res["shortfall"], 0)

    def test_shortfall_surfaced_when_unfillable(self):
        def fake(messages, model, base, api_key, timeout, num_predict):
            # Always returns the same 4-unique + 1-dup batch: never fillable.
            return json.dumps([RED, dict(RED), PHOTO, SPIDER, MERCURY])

        res = self._run(fake, count=5)
        self.assertEqual(len(res["accepted"]), 4)
        self.assertEqual(res["requested"], 5)
        self.assertEqual(res["shortfall"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
