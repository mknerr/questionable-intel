"""Regression tests for small generation counts (count=1,2).

Check 7 (answer-position distribution) caps the max-position share at 40 %,
which is arithmetically unsatisfiable below 3 questions — so
``_enforce_distribution`` must *skip* the rebalance/assert for n < 3 rather than
abort the job, while n >= 3 keeps enforcing unchanged. No network, deterministic.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gen


def question(stem, answer="Mars", options=("Mars", "Venus", "Jupiter", "Saturn")):
    return {"stem": stem, "options": list(options), "answer": answer,
            "confidence": "high", "category": "science"}


def answer_positions(qs):
    out = []
    for q in qs:
        idx = [gen._normalize(o) for o in q["options"]].index(gen._normalize(q["answer"]))
        out.append(idx)
    return out


class SmallCountDistributionTest(unittest.TestCase):
    def test_count_one_skips_rebalance(self):
        # n=1 used to raise AssertionError ("distribution still skewed").
        q = question("Which planet is known as the Red Planet?")
        out = gen._enforce_distribution([q])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["answer"], "Mars")
        self.assertEqual(out, [q])  # unchanged: rebalance skipped

    def test_count_two_skips_rebalance(self):
        # n=2 used to raise AssertionError ("distribution still skewed").
        qs = [question("Which planet is known as the Red Planet?"),
              question("Which planet is the largest gas giant?")]
        out = gen._enforce_distribution(qs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out, qs)  # unchanged: rebalance skipped

    def test_count_three_still_enforces_distribution(self):
        # n=3 must still run the round-robin rebalance: three answers all in
        # position 0 get spread to positions 0,1,2 (max share 1/3 <= 40%).
        qs = [question(f"Question number {i} about planets?") for i in range(3)]
        self.assertEqual(answer_positions(qs), [0, 0, 0])
        out = gen._enforce_distribution(qs)
        self.assertEqual(len(out), 3)
        positions = answer_positions(out)
        self.assertEqual(sorted(positions), [0, 1, 2])
        hist = {p: positions.count(p) for p in set(positions)}
        self.assertTrue(gen._distribution_ok(hist, len(out)))

    def test_distribution_cap_not_lowered_globally(self):
        # The 40 % cap is unchanged: 2 of 3 answers in one position (67 %) is
        # still skewed, proving the skip was not implemented as a blanket relax.
        self.assertFalse(gen._distribution_ok({0: 2, 1: 1}, 3))
        self.assertFalse(gen._distribution_ok({0: 3}, 3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
