#!/usr/bin/env python3
"""Closed-book baseline: feed only the question text to an LLM, no retrieved context.

The purpose is to establish the lower bound: whatever the LLM knows from training
about DNV rules, without any document retrieval at all. If HippoRAG/GraphRAG score
worse than this, they are actively hurting performance (noise > signal).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge
sys.path.insert(0, str(Path(__file__).parent))
from llm_client import LLMClient


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


PROMPT = """\
You are a ship-design verification assistant specializing in DNV ship classification \
rules. Answer the following question using your own knowledge of DNV requirements. \
Do NOT hedge or refuse — make your best attempt based on what you know.

## QUESTION
{question}

## INSTRUCTIONS
Give a concrete answer. If the question asks for a numeric value, compute it. If it \
asks about compliance, state compliant or not. Cite clause numbers when you can \
recall them.
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", default="/workspace/regbench_pilot/pilot_49_v7_with_gold_pages.json")
    parser.add_argument("--out", default="/workspace/regbench_pilot/pilot_49_v7_closedbook_results.json")
    parser.add_argument("--model", default="claude-sonnet-4-5-20250929")
    args = parser.parse_args()

    questions = json.loads(Path(args.input).read_text())
    log(f"Loaded {len(questions)} questions, model={args.model}")

    client = LLMClient(
        cache_dir="/workspace/regbench_pilot/closedbook_cache",
        token_log_path="/workspace/regbench_pilot/closedbook_token_log.jsonl",
    )

    results = []
    for i, q in enumerate(questions):
        prompt = PROMPT.format(question=q["question_text"])
        cache_key = f"closedbook_mt16k_{q['id']}_{args.model}"
        try:
            response = client.call_with_retry(
                messages=[{"role": "user", "content": prompt}],
                model=args.model, max_tokens=16384, temperature=0.0,
                cache_key=cache_key,
            )
        except Exception as e:
            response = f"[ERROR: {type(e).__name__}: {e}]"
        results.append({
            "id": q["id"],
            "tier": q["tier"],
            "condition": "closedbook",
            "question_text": q["question_text"],
            "required_facts": q.get("required_facts", []),
            "annotator_grounding": q.get("annotator_grounding", {}),
            "answer": response,
            "model": args.model,
        })
        log(f"  [{i+1}/{len(questions)}] T{q['tier']} {q['id']} ({len(response)} chars)")

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    log(f"Saved → {args.out}")
    log(f"Token usage: {client.get_token_usage()}")


if __name__ == "__main__":
    main()
