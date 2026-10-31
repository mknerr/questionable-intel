"""AI generation smoke test.

Generates N questions on a real topic against the configured LLM (DeepSeek by
default) and records model, prompt, wall-clock latency and retries.
Re-validates every accepted question independently against the gate and checks
the distribution (check 7).

Usage:  python3 smoke_gen.py [topic] [count]

Reads the same environment as the app: TRIVIA_LLM_API_KEY (required for
DeepSeek/OpenAI), TRIVIA_LLM_BASE and TRIVIA_LLM_MODEL (both optional).
"""
import os
import sys
import time

import gen
import settings


def main() -> int:
    topic = sys.argv[1] if len(sys.argv) > 1 else "1980s music"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    base = os.environ.get("TRIVIA_LLM_BASE") or gen.DEFAULT_BASE
    # Same fallback as settings.resolve: a non-default endpoint gets its own
    # preset's model, never DeepSeek's.
    preset = settings._preset_for_base(base) if base != gen.DEFAULT_BASE else None
    model = (os.environ.get("TRIVIA_LLM_MODEL")
             or (preset or {}).get("model_hint") or gen.DEFAULT_MODEL)
    api_key = os.environ.get("TRIVIA_LLM_API_KEY") or None

    # Same hosted-provider key check the server applies before generating.
    problem = settings.missing_key_error({"base_url": base, "api_key": api_key})
    if problem:
        print(problem, file=sys.stderr)
        print("Set TRIVIA_LLM_API_KEY in your environment and re-run.", file=sys.stderr)
        return 2

    print(f"model   : {model}")
    print(f"base    : {base}")
    print(f"topic   : {topic!r}   count: {count}")
    print(f"prompt  : (system) {gen._system_prompt()[:90].strip()}…")
    print(f"key     : {'set' if api_key else 'not set'}")
    print("thinking: disabled in the request body (see gen._request_body)")
    print("generating…", flush=True)

    t0 = time.time()
    res = gen.generate_questions(topic, count, model=model, base=base,
                                 api_key=api_key, timeout=240)
    wall = time.time() - t0

    accepted = res["accepted"]
    rejected = res["rejected"]
    print(f"\nwall-clock: {wall:.1f}s   accepted: {len(accepted)}   rejected: {len(rejected)}")

    # independent re-validation of every accepted question
    pool = set()
    hist = {}
    all_ok = True
    for q in accepted:
        ok, reason = gen._validate_question(q, pool)
        pool.add(gen._stem_key(q["stem"]))
        idx = [gen._normalize(o) for o in q["options"]].index(gen._normalize(q["answer"]))
        hist[idx] = hist.get(idx, 0) + 1
        if not ok:
            all_ok = False
            print(f"  !! revalidation failed for accepted question: {reason}")
    dist_ok = gen._distribution_ok(hist, len(accepted))

    print("\naccepted questions:")
    for i, q in enumerate(accepted):
        idx = [gen._normalize(o) for o in q["options"]].index(gen._normalize(q["answer"]))
        print(f"  {i+1}. [{q.get('confidence','?')}] {q['stem'][:70]}")
        print(f"      A-D: {q['options']}  (correct at pos {idx})")

    if rejected:
        print("\nrejected:")
        for r in rejected:
            print(f"  - {r['reason']} (attempts {r['attempts']})")

    print("\naudit trail:")
    for a in res["audit"]:
        print(f"  [{a['status']}] {a['reason'] or '-'}  {a['latency_ms']}ms  attempt {a['attempt']}")

    print(f"\ngate revalidation all-ok: {all_ok}   distribution-ok: {dist_ok}")
    assert all_ok, "gate revalidation failed"
    assert len(accepted) >= 5, "expected >= 5 accepted questions"
    print(f"\nPASS: {len(accepted)}/{count} questions generated and passed the gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
