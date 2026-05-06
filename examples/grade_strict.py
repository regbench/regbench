"""Strict atomic-fact conjunction grader for RegBench answers.

This is a minimal, self-contained reference implementation of the judge described
in the paper. An item is graded `is_correct = True` iff every proposition in
`required_facts` is judged `present` in the model's response by the LLM judge.

The default judge is Claude Sonnet 4.6 (matching the paper's primary judge).
Set `ANTHROPIC_API_KEY` in your environment before running.

Usage:
    python examples/grade_strict.py \\
        --predictions path/to/model_outputs.jsonl \\
        --out path/to/graded.jsonl

Each record in `predictions` must contain at minimum:
    {
      "id":             "<RegBench item id>",
      "question_text":  "<the prompt the model saw>",
      "required_facts": ["...", "..."],
      "answer":         "<the model's full response>",
      "tier":           <int>,
      "annotator_grounding": {"expected_derivation": "...", ...}   # optional
    }

The script appends `verdict` (full per-fact judge output) and `is_correct` (bool)
to each record and writes the result to `--out`.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


JUDGE_PROMPT = """\
You are grading an AI system's answer to a regulatory cross-reference question.

## QUESTION
{question}

## EXPECTED CORRECT DERIVATION (ground truth)
{expected}

## REQUIRED FACTS
The correct answer MUST contain equivalent statements of each of the following atomic facts.
Each fact is independently checkable against the model's response.

{required_facts}

## THE MODEL'S RESPONSE
{response}

## YOUR TASK
For each required fact, determine whether the model's response contains an equivalent \
statement (same numeric value, same decision, same clause being applied). Different \
wording is fine. Wrong numbers or inverted decisions are not.

Output a single JSON object (no fences):

{{
  "fact_verdicts": [
    {{"fact": "<fact verbatim>", "present": true|false, "evidence": "<short quote>"}}
  ],
  "facts_present": <int>,
  "facts_total":   <int>,
  "is_correct":    true|false,
  "rationale":     "<one-sentence rationale>"
}}

Mark `is_correct: true` only if ALL required facts are present.
"""


def _strip_to_json(raw: str) -> str:
    raw = raw.strip()
    if "<think>" in raw:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw


def _parse_judge_output(raw: str) -> dict:
    raw = _strip_to_json(raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {"is_correct": False, "error": "parse_fail", "raw": raw[:500]}
    try:
        return json.loads(raw[i : j + 1])
    except json.JSONDecodeError:
        return {"is_correct": False, "error": "json_fail", "raw": raw[:500]}


def call_judge(prompt: str, model: str) -> str:
    """Single call to the LLM judge.  Replace this if you use a different vendor."""
    from anthropic import Anthropic

    client = Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=16_384,
        temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def judge_one(item: dict, model: str) -> dict:
    required = item.get("required_facts") or []
    if not required:
        return {"is_correct": False, "error": "no_required_facts"}
    grounding = item.get("annotator_grounding") or {}
    expected = grounding.get("expected_derivation") or grounding.get("chain_summary") or ""
    prompt = JUDGE_PROMPT.format(
        question=item.get("question_text", ""),
        expected=expected,
        required_facts="\n".join(f"- {f}" for f in required),
        response=(item.get("answer") or ""),
    )
    raw = call_judge(prompt, model)
    return _parse_judge_output(raw)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True, help="JSONL of model outputs")
    ap.add_argument("--out", required=True, help="Where to write graded JSONL")
    ap.add_argument("--judge-model", default="claude-sonnet-4-6", help="LLM judge id")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    in_path = Path(args.predictions)
    out_path = Path(args.out)
    records = [json.loads(line) for line in in_path.read_text().splitlines() if line.strip()]
    print(f"loaded {len(records)} predictions from {in_path}")

    by_tier: dict[int, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    with out_path.open("w") as f:
        for i, rec in enumerate(records, 1):
            verdict = judge_one(rec, args.judge_model)
            rec["verdict"] = verdict
            rec["is_correct"] = bool(verdict.get("is_correct", False))
            tier = rec.get("tier", -1)
            by_tier[tier]["total"] += 1
            if rec["is_correct"]:
                by_tier[tier]["correct"] += 1
            mark = "✓" if rec["is_correct"] else "✗"
            fp = verdict.get("facts_present", "?")
            ft = verdict.get("facts_total", "?")
            print(f"  [{i}/{len(records)}] {mark} T{tier} {rec.get('id')} facts={fp}/{ft}")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\nwrote graded results -> {out_path}")
    print("\ntier  strict-correct")
    total_c = total_n = 0
    for t in sorted(by_tier):
        c, n = by_tier[t]["correct"], by_tier[t]["total"]
        total_c += c
        total_n += n
        print(f"T{t}    {c:>3}/{n:<3} ({100 * c / max(n, 1):5.1f}%)")
    print(f"OVERALL {total_c}/{total_n} ({100 * total_c / max(total_n, 1):.1f}%)")


if __name__ == "__main__":
    main()
