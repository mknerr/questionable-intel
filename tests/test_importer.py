"""importer.parse: every format, the rules around options/answers/limits, and
the guarantee that no text, however broken, raises. Pure functions, no I/O."""
import json
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gen
import importer


def parse(text, **kw):
    return importer.parse(text, **kw)


def one(text, **kw):
    r = parse(text, **kw)
    assert len(r["questions"]) == 1, r
    return r["questions"][0]


class JsonTest(unittest.TestCase):
    def test_array_with_aliases(self):
        r = parse(json.dumps([
            {"question": "What is the capital of France?", "correct_answer": "Paris",
             "choices": ["Rome", "Paris", "Berlin"], "round": "Geo", "points": 25},
            {"q": "Largest ocean on Earth?", "a": "Pacific"},
        ]))
        self.assertEqual(r["format"], "json")
        self.assertEqual(r["errors"], [])
        a, b = r["questions"]
        self.assertEqual((a["type"], a["options"], a["answer"], a["category"], a["points"]),
                         ("mc", ["Rome", "Paris", "Berlin"], "Paris", "Geo", 25))
        self.assertEqual((b["type"], b["status"], b["options"]), ("text", "complete", None))

    def test_object_with_questions_or_results(self):
        for key in ("questions", "results"):
            r = parse(json.dumps({key: [{"stem": "Who wrote Hamlet?", "answer": "Shakespeare"}]}))
            self.assertEqual(len(r["questions"]), 1, key)

    def test_open_trivia_db_shape_is_unescaped_and_shuffled(self):
        item = {"category": "Science &amp; Nature", "type": "multiple", "difficulty": "easy",
                "question": "What&#039;s H&lt;sub&gt;2&lt;/sub&gt;O?", "correct_answer": "Water &amp; ice",
                "incorrect_answers": ["Salt", "Sand", "Smoke"]}
        q = one(json.dumps({"response_code": 0, "results": [item]}))
        self.assertEqual(q["stem"], "What's H<sub>2</sub>O?")
        self.assertEqual(q["answer"], "Water & ice")
        self.assertEqual(q["category"], "Science & Nature")
        self.assertEqual(sorted(q["options"]), ["Salt", "Sand", "Smoke", "Water & ice"])
        self.assertIn(q["answer"], q["options"])

    def test_generator_output_round_trips(self):
        generated = gen._clean_question({
            "stem": "Which planet is known as the Red Planet?", "answer": "Mars",
            "options": ["Venus", "Mars", "Jupiter", "Saturn"], "confidence": "high",
            "media_url": "http://evil/x.png"})
        q = one(json.dumps([generated]))
        self.assertEqual(q["stem"], generated["stem"])
        self.assertEqual(q["options"], generated["options"])  # order kept
        self.assertEqual((q["answer"], q["type"], q["status"]), ("Mars", "mc", "complete"))

    def test_unlisted_fields_never_survive(self):
        q = one(json.dumps([{"stem": "Who wrote Hamlet?", "answer": "Shakespeare",
                             "media_url": "http://x/y.png", "media_type": "image",
                             "source": "x", "confidence": "high"}]))
        self.assertEqual(set(q), {"stem", "answer", "options", "type", "points",
                                  "category", "status", "line"})

    def test_options_dict_with_letter_answer(self):
        q = one(json.dumps([{"stem": "Pick the right one please", "answer": "B",
                             "options": {"A": "no", "B": "yes", "C": "maybe"}}]))
        self.assertEqual(q["answer"], "yes")

    def test_malformed_json_is_an_error_not_a_traceback(self):
        for text in ('[{"stem": "x"', '{"questions": [', "[1, 2,", "[bad json"):
            r = parse(text)
            self.assertEqual(r["questions"], [], text)
            self.assertTrue(r["errors"], text)
            self.assertEqual(r["format"], "json")

    def test_bracketed_prose_is_not_json(self):
        r = parse("[Music] Who painted the Mona Lisa?")
        self.assertEqual(r["format"], "lines")
        self.assertEqual(len(r["questions"]), 1)

    def test_non_object_items_and_empty_object(self):
        r = parse('[1, "x", {"stem": "Who wrote Hamlet?", "answer": "Shakespeare"}]')
        self.assertEqual(len(r["questions"]), 1)
        self.assertEqual(len(r["errors"]), 2)
        self.assertTrue(parse('{"foo": 1}')["errors"])

    def test_deep_nesting_does_not_raise(self):
        r = parse("[" * 100_000)
        self.assertEqual(r["questions"], [])
        self.assertTrue(r["errors"])


class LetteredTest(unittest.TestCase):
    def test_star_marker_and_numbering(self):
        q = one("1. What is the capital of France?\nA. Berlin\n*B. Paris\nC. Rome\nD. Madrid")
        self.assertEqual(q["stem"], "What is the capital of France?")
        self.assertEqual(q["options"], ["Berlin", "Paris", "Rome", "Madrid"])
        self.assertEqual(q["answer"], "Paris")

    def test_aiken_answer_line_and_case_variants(self):
        text = ("Q1: Which planet is red?\nA) Venus\nB) Mars\nC) Jupiter\nANSWER: B\n\n"
                "Largest ocean on Earth?\na) Atlantic\nb) Pacific\nc) Indian\nAnswer: b")
        r = parse(text)
        self.assertEqual(r["format"], "lettered")
        self.assertEqual([q["answer"] for q in r["questions"]], ["Mars", "Pacific"])
        self.assertEqual(r["questions"][0]["stem"], "Which planet is red?")

    def test_blank_lines_between_blocks_are_optional(self):
        text = ("First question here?\nA. one\nB. two\nANSWER: A\n"
                "Second question here?\nA. three\nB. four\nANSWER: B")
        self.assertEqual([q["answer"] for q in parse(text)["questions"]], ["one", "four"])

    def test_no_answer_marked_needs_answer_and_keeps_options(self):
        q = one("Which planet is red?\nA. Venus\nB. Mars")
        self.assertEqual((q["status"], q["options"], q["type"]),
                         ("needs_answer", ["Venus", "Mars"], "mc"))

    def test_star_and_answer_line_disagreeing_is_an_error(self):
        r = parse("Which planet is red?\nA. Venus\n*B. Mars\nANSWER: A")
        self.assertEqual(r["questions"], [])
        self.assertTrue(r["errors"])

    def test_option_order_is_kept(self):
        q = one("Pick the first letter?\nA. zebra\nB. apple\nC. mango\n*D. kiwi")
        self.assertEqual(q["options"], ["zebra", "apple", "mango", "kiwi"])

    def test_lettered_beats_tagged(self):
        r = parse("Which planet is red?\nA. Venus\nB. Mars\nAnswer: B")
        self.assertEqual(r["format"], "lettered")


class TaggedTest(unittest.TestCase):
    def test_q_a_pairs_case_insensitive(self):
        r = parse("Q: Who wrote Hamlet?\nA: Shakespeare\nquestion: Largest ocean?\nANSWER: Pacific")
        self.assertEqual(r["format"], "tagged")
        self.assertEqual([(q["stem"], q["answer"]) for q in r["questions"]],
                         [("Who wrote Hamlet?", "Shakespeare"), ("Largest ocean?", "Pacific")])

    def test_answer_only_and_question_only_rows(self):
        r = parse("A: Everest\n\nQ: Largest ocean?\n\nQ: Who wrote Hamlet?\nA: Shakespeare")
        self.assertEqual([q["status"] for q in r["questions"]],
                         ["needs_question", "needs_answer", "complete"])

    def test_wrong_answers_become_shuffled_options(self):
        q = one("Q: Who wrote Hamlet?\nA: Shakespeare\nWrong: Marlowe\nW: Bacon\nW: Jonson")
        self.assertEqual(q["type"], "mc")
        self.assertEqual(sorted(q["options"]), ["Bacon", "Jonson", "Marlowe", "Shakespeare"])

    def test_wrong_without_answer_is_ignored_with_a_message(self):
        r = parse("Q: Who wrote Hamlet?\nW: Marlowe")
        self.assertEqual(r["questions"][0]["status"], "needs_answer")
        self.assertTrue(r["errors"])

    def test_wrapped_line_continues_and_category_applies(self):
        r = parse("Category: Books\nQ: Who wrote\nthe play Hamlet?\nA: Shakespeare")
        q = r["questions"][0]
        self.assertEqual(q["stem"], "Who wrote the play Hamlet?")
        self.assertEqual(q["category"], "Books")

    def test_tagged_is_not_confused_with_lettered(self):
        # "A:" (tagged) vs "A." / "A)" (lettered): different formats.
        self.assertEqual(parse("Q: One thing?\nA: yes")["format"], "tagged")
        self.assertEqual(parse("One thing?\nA. yes\nB. no\nANSWER: A")["format"], "lettered")


class DelimitedTest(unittest.TestCase):
    def test_quizlet_tab_export(self):
        r = parse("Mitochondria\tPowerhouse of the cell\nNucleus\tHolds the DNA\n")
        self.assertEqual(r["format"], "tsv")
        self.assertEqual([(q["stem"], q["answer"], q["type"]) for q in r["questions"]],
                         [("Mitochondria", "Powerhouse of the cell", "text"),
                          ("Nucleus", "Holds the DNA", "text")])

    def test_quizlet_semicolon_cards(self):
        r = parse("Paris\tCapital of France;Berlin\tCapital of Germany;Rome\tCapital of Italy")
        self.assertEqual(r["format"], "tsv")
        self.assertEqual(len(r["questions"]), 3)
        self.assertEqual(r["questions"][1]["answer"], "Capital of Germany")

    def test_swap_flips_two_column_rows(self):
        text = "Paris\tCapital of France\nBerlin\tCapital of Germany"
        q = parse(text, swap=True)["questions"][0]
        self.assertEqual((q["stem"], q["answer"]), ("Capital of France", "Paris"))

    def test_swap_ignores_rows_with_options(self):
        text = "Who wrote Hamlet?,Shakespeare,Marlowe,Bacon\nWho wrote Emma?,Austen,Bronte,Eliot"
        q = parse(text, swap=True)["questions"][0]
        self.assertEqual((q["stem"], q["answer"]), ("Who wrote Hamlet?", "Shakespeare"))

    def test_spreadsheet_header_with_extra_columns(self):
        text = ("Question\tAnswer\tCategory\tPoints\tNotes\n"
                "Who wrote Hamlet?\tShakespeare\tBooks\t20\tclassic\n")
        q = one(text)
        self.assertEqual((q["category"], q["points"]), ("Books", 20))

    def test_csv_header_aliases(self):
        for header in ("question,answer", "stem,correct", "Q,A", "term,definition",
                       "prompt,correct answer", "Question,Correct_Answer"):
            q = one(header + "\nWho wrote Hamlet?,Shakespeare")
            self.assertEqual((q["stem"], q["answer"]), ("Who wrote Hamlet?", "Shakespeare"), header)

    def test_csv_wrong_columns(self):
        for header in ("question,answer,wrong1,wrong2,wrong3",
                       "Question,Correct,Wrong Answer 1,Wrong Answer 2,Wrong Answer 3",
                       "q,a,incorrect1,incorrect2,incorrect3"):
            q = one(header + "\nWho wrote Hamlet?,Shakespeare,Marlowe,Bacon,Jonson")
            self.assertEqual(sorted(q["options"]),
                             ["Bacon", "Jonson", "Marlowe", "Shakespeare"], header)

    def test_csv_option_columns_with_letter_answer_and_order(self):
        for header in ("question,option a,option b,option c,answer",
                       "question,choice 1,choice 2,choice 3,answer",
                       "question,a,b,c,answer"):
            q = one(header + "\nWho wrote Hamlet?,Marlowe,Shakespeare,Bacon,B")
            self.assertEqual(q["options"], ["Marlowe", "Shakespeare", "Bacon"], header)
            self.assertEqual(q["answer"], "Shakespeare", header)

    def test_csv_quotes_and_embedded_commas_and_newlines(self):
        text = 'question,answer\n"Who wrote ""Hamlet"", the play?","Shakespeare, William"\n"two\nlines?",yes'
        r = parse(text)
        self.assertEqual(r["questions"][0]["stem"], 'Who wrote "Hamlet", the play?')
        self.assertEqual(r["questions"][0]["answer"], "Shakespeare, William")
        self.assertEqual(r["questions"][1]["stem"], "two lines?")

    def test_csv_type_and_points_columns(self):
        r = parse("question,answer,type,points\nHow many legs on a spider?,8,numeric,5000\n"
                  "Name a primary colour,red,text,0")
        a, b = r["questions"]
        self.assertEqual((a["type"], a["points"]), ("numeric", 1000))
        self.assertEqual((b["type"], b["points"]), ("text", 1))

    def test_headerless_csv_columns_are_question_answer_wrong(self):
        r = parse("Who wrote Hamlet?,Shakespeare,Marlowe\nWho wrote Emma?,Austen,Bronte")
        self.assertEqual(r["format"], "csv")
        self.assertEqual(sorted(r["questions"][0]["options"]), ["Marlowe", "Shakespeare"])

    def test_headerless_csv_with_ragged_columns_is_not_csv(self):
        r = parse("Who wrote Hamlet?,Shakespeare\nWho wrote Emma?,Austen,Bronte")
        self.assertEqual(r["format"], "lines")

    def test_comma_in_a_question_is_not_a_table(self):
        for text in ("In 1969, who walked on the moon?",
                     "Who was Napoleon, the emperor?\nWho was Caesar, the dictator?",
                     "Name the capital of France, please",
                     "Which river, longer than any other in Europe, flows through Russia?"):
            r = parse(text)
            self.assertEqual(r["format"], "lines", text)
            self.assertTrue(all(q["status"] == "needs_answer" for q in r["questions"]), text)

    def test_only_header_is_not_an_import(self):
        r = parse("question,answer")
        self.assertEqual(r["questions"], [])
        self.assertTrue(r["errors"])

    def test_mixed_paste_with_a_minority_of_tab_rows_keeps_its_columns(self):
        r = importer.parse("Capital of France\tParis\nLargest ocean on Earth?\nMount Everest\nMont Blanc")
        self.assertEqual(r["format"], "tsv")
        self.assertEqual([(q["stem"], q["answer"], q["status"]) for q in r["questions"]], [
            ("Capital of France", "Paris", "complete"),
            ("Largest ocean on Earth?", None, "needs_answer"),
            (None, "Mount Everest", "needs_question"),
            (None, "Mont Blanc", "needs_question"),
        ])

    def test_tab_row_without_answer_is_needs_answer(self):
        r = parse("Who wrote Hamlet?\t\nParis\tCapital of France")
        self.assertEqual([q["status"] for q in r["questions"]], ["needs_answer", "complete"])


class MixedPasteTest(unittest.TestCase):
    """Hosts paste questions and/or answers together; nothing may be glued up."""

    def rows(self, text):
        return [(q["stem"], q["answer"], q["status"]) for q in importer.parse(text)["questions"]]

    def test_bare_lines_after_q_a_blocks_are_their_own_rows(self):
        self.assertEqual(self.rows("Q: Tallest mountain?\nA: Everest\nHow far is the moon?\nMount Fuji"), [
            ("Tallest mountain?", "Everest", "complete"),
            ("How far is the moon?", None, "needs_answer"),
            (None, "Mount Fuji", "needs_question"),
        ])

    def test_many_bare_lines_with_one_q_a_block(self):
        rows = self.rows("Q: Tallest mountain?\nA: Everest\nParis\nTokyo\nBerlin\nRome\nCairo")
        self.assertEqual(rows[0], ("Tallest mountain?", "Everest", "complete"))
        self.assertEqual(rows[1:], [(None, c, "needs_question")
                                    for c in ("Paris", "Tokyo", "Berlin", "Rome", "Cairo")])

    def test_wrapped_fields_still_continue(self):
        self.assertEqual(self.rows("Q: Which river is\nthe longest in Africa?\nA: The Nile\n  in Egypt"), [
            ("Which river is the longest in Africa?", "The Nile in Egypt", "complete"),
        ])

    def test_bare_lines_around_lettered_blocks_are_their_own_rows(self):
        self.assertEqual(self.rows(
            "How far is the moon?\n\nWhich planet is largest?\nA) Mars\nB) Jupiter\nANSWER: B\n\n"
            "Who painted the Mona Lisa?\nDa Vinci"), [
            ("How far is the moon?", None, "needs_answer"),
            ("Which planet is largest?", "Jupiter", "complete"),
            ("Who painted the Mona Lisa?", None, "needs_answer"),
            (None, "Da Vinci", "needs_question"),
        ])

    def test_two_line_lettered_stem_still_joins(self):
        self.assertEqual(self.rows("Of these planets,\nwhich is largest?\nA) Mars\nB) Jupiter\nANSWER: B"), [
            ("Of these planets, which is largest?", "Jupiter", "complete"),
        ])


class BareLinesTest(unittest.TestCase):
    def kinds(self, *lines):
        r = parse("\n".join(lines))
        self.assertEqual(r["format"], "lines")
        return [q["status"] for q in r["questions"]]

    def test_classifier(self):
        got = self.kinds(
            "What is the capital of France?",       # ends in ?
            "Name the largest ocean",               # interrogative start
            "In what year did the war end",         # "in what"
            "Does the sun rise in the east",        # interrogative start
            "The tallest mountain on the Earth is the one in Nepal",  # >= 8 words
            "Paris", "Pacific Ocean", "3.14", "Mount Everest")
        self.assertEqual(got, ["needs_answer"] * 5 + ["needs_question"] * 4)

    def test_leading_numbering_and_bullets_are_stripped(self):
        r = parse("1. Who wrote Hamlet?\n2) Paris\n- Berlin\n3.14\n2-3 players")
        got = [(q["stem"], q["answer"]) for q in r["questions"]]
        self.assertEqual(got, [("Who wrote Hamlet?", None), (None, "Paris"), (None, "Berlin"),
                               (None, "3.14"), (None, "2-3 players")])

    def test_answer_only_rows_have_no_stem(self):
        q = one("Pacific Ocean")
        self.assertIsNone(q["stem"])
        self.assertEqual(q["status"], "needs_question")


class RulesTest(unittest.TestCase):
    def test_letter_reference_resolves_when_not_option_text(self):
        for answer in ("B", "b", "(B)", "B.", "B)"):
            q = one(f"question,option a,option b,option c,answer\nPick one of these?,red,green,blue,{answer}")
            self.assertEqual(q["answer"], "green", answer)

    def test_letter_that_is_also_option_text_matches_the_text(self):
        q = one("question,option a,option b,answer\nPick one of these?,B,A,A")
        self.assertEqual(q["answer"], "A")

    def test_unresolvable_answer_drops_the_row_with_a_line_error(self):
        r = parse("question,option a,option b,answer\nPick one of these?,red,green,purple")
        self.assertEqual(r["questions"], [])
        self.assertEqual(r["errors"][0]["line"], 2)

    def test_answer_matches_options_the_way_the_engine_does(self):
        q = one(json.dumps([{"stem": "Pick one of these?", "answer": " the  RED! ",
                             "options": ["The Red", "Green"]}]))
        self.assertEqual(q["answer"], "The Red")

    def test_shuffle_keeps_the_answer_and_moves_it(self):
        text = "question,answer,wrong1,wrong2,wrong3\n" + "\n".join(
            f"Question number {i} is?,right{i},a{i},b{i},c{i}" for i in range(40))
        random.seed(7)
        qs = parse(text)["questions"]
        positions = set()
        for i, q in enumerate(qs):
            self.assertIn(f"right{i}", q["options"])
            self.assertEqual(q["answer"], f"right{i}")
            positions.add(q["options"].index(q["answer"]))
        self.assertGreater(len(positions), 1)

    def test_explicit_lists_are_never_shuffled(self):
        with mock.patch.object(importer.random, "shuffle", side_effect=AssertionError):
            one(json.dumps([{"stem": "Pick one of these?", "answer": "b", "options": ["a", "b", "c"]}]))
            one("Pick one of these?\nA. a\n*B. b\nC. c")

    def test_text_for_question_and_answer_without_options(self):
        q = one("Who wrote Hamlet?\tShakespeare")
        self.assertEqual((q["type"], q["status"], q["options"]), ("text", "complete", None))

    def test_explicit_type_rules(self):
        self.assertEqual(one("question,answer,type\nHow many legs on a spider?,8,numeric")["type"], "numeric")
        bad = parse("question,answer,type\nHow many legs on a spider?,eight,numeric")
        self.assertEqual(bad["questions"], [])
        self.assertTrue(bad["errors"])
        bad = parse("question,answer,type\nWho wrote Hamlet?,Shakespeare,mc")
        self.assertEqual(bad["questions"], [])
        q = one(json.dumps([{"stem": "Who wrote Hamlet?", "answer": "Shakespeare", "type": "text",
                             "options": ["Shakespeare", "Marlowe"]}]))
        self.assertEqual((q["type"], q["options"], q["answer"]), ("text", None, "Shakespeare"))
        # Open Trivia DB's own type names map onto ours.
        self.assertEqual(one(json.dumps([{"question": "Is the sky blue?", "type": "boolean",
                                          "correct_answer": "True",
                                          "incorrect_answers": ["False"]}]))["type"], "mc")

    def test_limits(self):
        long_stem = "x" * (importer.STEM_MAX + 1)
        cases = {
            "stem too short": [{"stem": "ab", "answer": "x"}],
            "stem too long": [{"stem": long_stem, "answer": "x"}],
            "answer too long": [{"stem": "Fine question?", "answer": "y" * (importer.ANSWER_MAX + 1)}],
            "one option": [{"stem": "Fine question?", "answer": "a", "options": ["a"]}],
            "seven options": [{"stem": "Fine question?", "answer": "a",
                               "options": list("abcdefg")}],
            "option too long": [{"stem": "Fine question?", "answer": "a",
                                 "options": ["a", "b" * (importer.OPTION_LEN_MAX + 1)]}],
            "duplicate options": [{"stem": "Fine question?", "answer": "a",
                                   "options": ["a", "A!", "b"]}],
        }
        for name, rows in cases.items():
            r = parse(json.dumps(rows))
            self.assertEqual(r["questions"], [], name)
            self.assertTrue(r["errors"], name)
        # The boundaries themselves are fine.
        ok = [{"stem": "x" * importer.STEM_MAX, "answer": "y" * importer.ANSWER_MAX},
              {"stem": "abc", "answer": "a", "options": list("abcdef"), "points": "7"}]
        self.assertEqual(len(parse(json.dumps(ok))["questions"]), 2)

    def test_points_default_and_clamp(self):
        pts = [parse(json.dumps([{"stem": "Fine question?", "answer": "a", "points": p}]))
               ["questions"][0]["points"] for p in (None, "x", 0, -5, 5000, 25, "30", 12.9, True)]
        self.assertEqual(pts, [10, 10, 1, 1, 1000, 25, 30, 12, 10])

    def test_question_cap(self):
        text = "\n".join(f"Question number {i} here?" for i in range(importer.MAX_IMPORT_QUESTIONS + 15))
        r = parse(text)
        self.assertEqual(len(r["questions"]), importer.MAX_IMPORT_QUESTIONS)
        self.assertIn("15 more", r["errors"][-1]["message"])

    def test_character_cap(self):
        r = parse("a" * (importer.MAX_IMPORT_CHARS + 1))
        self.assertEqual(r["questions"], [])
        self.assertIn("limit", r["errors"][0]["message"])
        self.assertEqual(len(parse("What is this?\n" * 10)["questions"]), 1)  # dedupe, not a cap

    def test_duplicate_stems_are_skipped_with_a_line_error(self):
        r = parse("Who wrote Hamlet?\tShakespeare\nwho wrote  HAMLET\tBacon")
        self.assertEqual(len(r["questions"]), 1)
        self.assertEqual(r["errors"][0]["line"], 2)

    def test_blank_input_says_what_to_paste(self):
        for text in ("", "   \n\t \n", "\ufeff\n"):
            r = parse(text)
            self.assertEqual(r["questions"], [])
            self.assertIn("Paste", r["errors"][0]["message"])
        self.assertEqual(parse(None)["questions"], [])

    def test_bom_and_crlf(self):
        text = "\ufeffQ: Who wrote Hamlet?\r\nA: Shakespeare\r\n\r\nQ: Largest ocean?\r\nA: Pacific\r\n"
        r = parse(text)
        self.assertEqual([(q["stem"], q["answer"]) for q in r["questions"]],
                         [("Who wrote Hamlet?", "Shakespeare"), ("Largest ocean?", "Pacific")])
        self.assertEqual(r["questions"][1]["line"], 4)
        self.assertEqual(len(parse("\ufeffabc\tdef\r\nghi\tjkl")["questions"]), 2)

    def test_error_list_is_capped(self):
        r = parse(json.dumps(["nope"] * 500))
        self.assertLessEqual(len(r["errors"]), importer.MAX_ERRORS + 1)

    def test_control_characters_and_whitespace_are_cleaned(self):
        q = one("Who  wrote\x00 Hamlet?\t Shakespeare \x07")
        self.assertEqual((q["stem"], q["answer"]), ("Who wrote Hamlet?", "Shakespeare"))

    def test_counts(self):
        r = parse("Q: Who wrote Hamlet?\nA: Shakespeare\n\nQ: Largest ocean?\n\nA: Everest")
        self.assertEqual(importer.counts(r["questions"]),
                         {"total": 3, "ready": 1, "needs_answer": 1, "needs_question": 1})


class FuzzTest(unittest.TestCase):
    FRAGMENTS = ["Q:", "A:", "W:", "A.", "B)", "*C.", "ANSWER: B", "\t", ",", ";", '"', "\n", "\n\n",
                 "\r", "[", "]", "{", "}", ":", "?", "1.", "question,answer", "wrong1", "option a",
                 "Who", "what is", "Paris", "\ufeff", "\x00", "é中\U0001f600", " ", "-", "'",
                 '{"stem":', "null", "\\", "&amp;", "99999999999999999999", "type", "numeric"]

    def test_random_garbage_never_raises(self):
        rng = random.Random(1234)
        for _ in range(600):
            text = "".join(rng.choice(self.FRAGMENTS) for _ in range(rng.randint(0, 40)))
            r = parse(text, swap=rng.random() < .5)
            self.assertEqual(set(r), {"format", "questions", "errors"}, repr(text))
            for q in r["questions"]:
                self.assertIn(q["status"], ("complete", "needs_answer", "needs_question"))
                self.assertTrue(q["stem"] or q["answer"])

    def test_random_bytes_never_raise(self):
        rng = random.Random(99)
        for _ in range(200):
            text = "".join(chr(rng.randint(0, 0x2FF)) for _ in range(rng.randint(0, 300)))
            parse(text)

    def test_hostile_shapes(self):
        for text in ('{"questions": {"a": 1}}', '{"results": null}', '[[], {}, null, true]',
                     '[{"stem": ["x"], "answer": {"y": 1}}]', '[{"stem": "Fine question?", "answer": "a", "options": 5}]',
                     '[{"stem": "Fine question?", "options": [null, {}, 1], "answer": 1}]',
                     '"just a string"', "12345", "null", "{" * 5000, "\t" * 5000, "," * 5000,
                     "A. \nB. \n" * 3000, 'question,answer\n"unterminated,x', "x\t" * 100_000):
            parse(text)

    def test_very_wide_csv_row(self):
        parse("question," + ",".join(f"wrong{i}" for i in range(500)) + "\n" + "x," * 500)


if __name__ == "__main__":
    unittest.main()
