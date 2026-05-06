#!/usr/bin/env python3
"""Judge RAG method results against the v7 required_facts, using the same LLM judge as the oracle baseline.

Input: a RAG result JSON with schema [{id, tier, question_text, required_facts, answer, ...}, ...]
Output: same file with added 'verdict' and 'is_correct' fields per question.
"""
import argparse, json, os, re, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge
sys.path.insert(0, str(Path(__file__).parent))
from llm_client import LLMClient


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


JUDGE_PROMPT = """\
You are grading an AI system's answer to a DNV ship-design question.

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
    {{"fact": "<fact verbatim>", "present": true|false, "evidence": "<short quote>"}},
    ...
  ],
  "facts_present": <int>,
  "facts_total": <int>,
  "is_correct": true|false,
  "rationale": "<one-sentence rationale>"
}}

Mark `is_correct: true` only if ALL required facts are present.
"""


def strip_to_json(raw: str) -> str:
    raw = raw.strip()
    if "<think>" in raw:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw


def judge_one(q: dict, client: LLMClient, judge_model: str, condition_tag: str) -> dict:
    required = q.get("required_facts") or []
    if not required:
        return {"is_correct": False, "error": "no_required_facts"}
    grounding = q.get("annotator_grounding", {}) or {}
    expected = grounding.get("expected_derivation") or grounding.get("chain_summary") or ""
    req_str = "\n".join(f"- {f}" for f in required)
    prompt = JUDGE_PROMPT.format(
        question=q.get("question_text", ""),
        expected=expected,
        required_facts=req_str,
        response=(q.get("answer") or ""),
    )
    cache_key = f"judge_rag_mt16k_{condition_tag}_{q['id']}_{judge_model}"
    raw = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=judge_model, max_tokens=16384, temperature=0.0,
        cache_key=cache_key,
    )
    raw = strip_to_json(raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {"is_correct": False, "error": "parse_fail", "raw": raw[:500]}
    try:
        return json.loads(raw[i:j + 1])
    except json.JSONDecodeError:
        return {"is_correct": False, "error": "json_fail", "raw": raw[:500]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", required=True)
    parser.add_argument("--tag", required=True, help="Short tag for cache keys, e.g. 'hipporag'")
    parser.add_argument("--judge", default="claude-sonnet-4-5-20250929",
                        help="Judge LLM. claude-sonnet-4-5-20250929 is the only sonnet currently "
                             "accessible via shubiaobiao.")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    out_path = Path(args.out or args.input)
    data = json.loads(Path(args.input).read_text())
    log(f"Loaded {len(data)} records from {args.input}")

    client = LLMClient(
        cache_dir="/workspace/regbench_pilot/rag_judge_cache",
        token_log_path="/workspace/regbench_pilot/rag_judge_token_log.jsonl",
    )

    for i, r in enumerate(data):
        verdict = judge_one(r, client, args.judge, args.tag)
        r["verdict"] = verdict
        r["is_correct"] = bool(verdict.get("is_correct", False))
        mark = "✓" if r["is_correct"] else "✗"
        fp = verdict.get("facts_present", "?")
        ft = verdict.get("facts_total", "?")
        log(f"  [{i+1}/{len(data)}] {mark} T{r['tier']} {r['id']} facts={fp}/{ft}")

    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log(f"\nSaved → {out_path}")

    # Aggregate
    by_tier = defaultdict(lambda: {"correct": 0, "total": 0})
    for r in data:
        t = r["tier"]
        by_tier[t]["total"] += 1
        if r["is_correct"]:
            by_tier[t]["correct"] += 1
    log(f"\n{'='*50}")
    log(f"Tier  correct")
    for t in sorted(by_tier):
        s = by_tier[t]
        acc = 100 * s["correct"] / max(s["total"], 1)
        log(f"T{t}   {s['correct']:>2}/{s['total']:<2} ({acc:5.1f}%)")
    tc = sum(s["correct"] for s in by_tier.values())
    tn = sum(s["total"] for s in by_tier.values())
    log(f"TOTAL {tc}/{tn} ({100*tc/max(tn,1):.1f}%)")


if __name__ == "__main__":
    main()
