# Importing questions

Host console, **Import questions** panel. Paste text or choose a `.txt`, `.csv`,
`.tsv` or `.json` file (up to 200 KB, 100 questions at a time), then **Preview**
to see what was understood and **Save as round**. The saved round shows up in
**Rounds** when you pick **Library**.

The format is detected for you. Nothing is saved until you press Save, and
Preview shows the line number of anything that could not be read.

## With no AI at all

A row with a question **and** an answer is ready and playable. Without
multiple-choice options it is a typed question: teams type their answer and you
judge it from the console, like any other typed question. Rows missing a
question or an answer are skipped when you save (use AI below to finish them).

## Formats

**One per line.** A line ending in `?`, starting with a question word (who, what,
when, where, why, which, how, name...) or of eight or more words is a question
with no answer yet. Anything else is an answer with no question yet.

```
Who painted the Mona Lisa?
What is the capital of Australia?
```

**Q: / A: blocks.** `Question:` and `Answer:` work too, in any case. `Wrong:` (or
`W:`) lines add wrong choices; the choices are shuffled. A lone `A:` is an answer
with no question. `Category:` and `Points:` lines are optional.

```
Q: What is the largest ocean?
A: Pacific
W: Atlantic
W: Indian
W: Arctic
```

**Lettered choices** (also Moodle's Aiken format). Mark the right one with `*`
or add an `ANSWER:` line. Choices stay in the order you wrote them.

```
1. Which planet is known as the Red Planet?
A. Venus
*B. Mars
C. Jupiter
D. Saturn

Which gas do plants absorb?
a) Oxygen
b) Carbon dioxide
c) Nitrogen
ANSWER: B
```

**Tab- or comma-separated.** The first column is the question, the second the
answer, and any further columns are wrong choices (shuffled with the answer).

```
Capital of Australia	Canberra
Largest ocean	Pacific	Atlantic	Indian
```

**Column headers (optional).** A first row with recognised names tells the
importer which column is which, in any order, with any extra columns ignored:

| Meaning | Header names |
|---|---|
| question | `question`, `stem`, `q`, `term`, `prompt` |
| answer | `answer`, `correct`, `correct answer`, `a`, `definition` |
| wrong choices | `wrong1`, `wrong2`..., `wrong answer 1`, `incorrect1`... |
| all the choices, in order | `option a`..`option f`, `choice 1`.., or columns `a`, `b`, `c`, `d` |
| category | `category`, `round`, `topic` |
| type | `type` (`mc`, `text`, `numeric`) |
| points | `points` |

With choice columns, the answer can be the text or its letter (`B`).

```
question,answer,wrong1,wrong2,wrong3,category,points
What is the largest ocean?,Pacific,Atlantic,Indian,Arctic,Geography,20
```

**JSON.** An array of objects, or an object with a `questions` (or `results`)
array. Field names: `stem`/`question`/`q`, `answer`/`correct_answer`/`a`,
`options`/`choices`, `category`, `type`, `points`. Open Trivia DB downloads work
as they are. The app's own generated questions round-trip.

```json
[{"stem": "Which planet is red?", "options": ["Venus", "Mars", "Jupiter"],
  "answer": "Mars", "category": "Space", "points": 10}]
```

Category and points are optional everywhere. Points default to 10 and are kept
between 1 and 1000. Rows with different categories are saved as separate rounds;
rows with none use the **Round name** you typed.

## From Quizlet

1. In Quizlet, open the set, then **...** > **Export**.
2. Leave the defaults (a **tab** between term and definition, a **new line**
   between cards) and press **Copy text**.
3. Paste into the panel and **Preview**.

If the questions and answers are the wrong way round (Quizlet's "term" is often
the answer), tick **Swap question / answer**. Quizlet's *semicolon between
cards* option works too.

## With an AI model

If an AI model is set up (AI settings below the console), two extra buttons help.
Neither is needed to save and play.

- **Let AI fill the gaps.** Finishes rows that are half done: a question with no
  answer, an answer with no question, or a question and answer with no wrong
  choices. Your question and answer text is kept word for word. Rows that are
  already complete multiple-choice pass through untouched. The result is saved
  straight into a round.
- **Write questions from this text.** Paste notes, an article or a list of facts
  and the AI writes the number of questions you ask for (1 to 40), drawn only
  from that text. Only the first 30,000 characters are used.

The AI's answers are **not fact-checked**; skim the new questions before playing.
Questions the checks reject (wrong answer not among the choices, answer given
away in the question, too similar to another, the AI unsure) are listed with a
reason and not saved. If the AI cannot be reached, Save as round still works.

**Tip:** any chatbot can produce the JSON format. Ask it for "20 trivia questions
about X as a JSON array of objects with `stem`, `options` (4 strings), `answer`
(one of the options, word for word) and `category`", paste the reply into the
panel and Preview.
