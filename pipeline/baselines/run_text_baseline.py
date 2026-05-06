#!/usr/bin/env python3
"""
Text dual-condition baseline for v7 questions.

Ports the previous v6 text dual-condition logic onto the v7 question format
(chain hidden, required_facts, annotator_grounding). Uses LLM-as-judge against
required_facts for scoring.

Conditions:
  - oracle       : chain sections' markdown text only
  - full_context : expand outward (anchor chapter → part → involved parts), cap 150K tokens

Text is read from MinerU's polished markdown (`<section>.md`) by default.
Pass --raw to use the unpolished `_raw.md` (realistic OCR).
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge

sys.path.insert(0, str(Path(__file__).parent))
from llm_client import LLMClient

PT1_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt1")
PT3_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt3")
PT5_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt5")

# Sonnet 4.6 has 200K-token context. 150K tokens ≈ 600K characters for English markdown.
# Leaves ~50K headroom for question, prompt scaffolding, and response.
# Can be overridden with REGBENCH_MAX_CONTEXT_CHARS env var for smaller-context models.
import os as _os
MAX_CONTEXT_CHARS = int(_os.environ.get("REGBENCH_MAX_CONTEXT_CHARS", "600000"))


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Section IO
# ---------------------------------------------------------------------------

def parse_section(sec_id: str) -> tuple[int, int, int] | None:
    m = re.match(r'Pt(\d+)\.Ch(\d+)\.Sec(\d+)', sec_id)
    if not m: return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def section_to_dir(sec_id: str) -> Path | None:
    p = parse_section(sec_id)
    if not p: return None
    pt, ch, sec = p
    base_map = {1: PT1_DIR, 3: PT3_DIR, 5: PT5_DIR}
    base = base_map.get(pt)
    if not base: return None
    return base / f"DNV-RU-Pt{pt}-Chap{ch}-sec{sec}" / "pipeline"


def load_section_text(sec_id: str, use_raw: bool = False) -> str:
    pipeline = section_to_dir(sec_id)
    if not pipeline or not pipeline.exists():
        return ""
    suffix = "_raw.md" if use_raw else ".md"
    for f in pipeline.glob(f"*{suffix}"):
        if use_raw:
            return f.read_text(errors="replace")
        if "_raw" not in f.name and "_content" not in f.name:
            return f.read_text(errors="replace")
    return ""


def list_sections_in(part: int, chapter: int | None = None) -> list[str]:
    base_map = {1: PT1_DIR, 3: PT3_DIR, 5: PT5_DIR}
    base = base_map.get(part)
    if not base or not base.exists(): return []
    out = []
    for d in sorted(base.iterdir()):
        m = re.match(r'DNV-RU-Pt(\d+)-Chap(\d+)-sec(\d+)', d.name)
        if not m: continue
        p, c, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if chapter is not None and c != chapter:
            continue
        out.append((p, c, s, f"Pt{p}.Ch{c}.Sec{s}"))
    out.sort()
    return [sid for _, _, _, sid in out]


# ---------------------------------------------------------------------------
# Context collection
# ---------------------------------------------------------------------------

def collect_oracle_context(question: dict, use_raw: bool = False) -> tuple[str, list[str]]:
    """Chain sections in natural document order."""
    sections = {question["source_section"]}
    if "chain" in question:
        for n in question["chain"].get("path", []):
            sections.add(n)

    def key(sid):
        p = parse_section(sid)
        return p if p else (99, 99, 99)
    ordered = sorted(sections, key=key)

    parts, used = [], []
    total = 0
    for sid in ordered:
        text = load_section_text(sid, use_raw=use_raw)
        if not text: continue
        if total + len(text) > MAX_CONTEXT_CHARS:
            text = text[: MAX_CONTEXT_CHARS - total] + "\n[truncated]"
        parts.append(f"## {sid}\n\n{text}")
        used.append(sid)
        total += len(text)
        if total >= MAX_CONTEXT_CHARS:
            break
    return "\n\n".join(parts), used


def collect_full_context(question: dict, use_raw: bool = False) -> tuple[str, list[str]]:
    """Expand outward from anchor section, document order, capped at MAX_CONTEXT_CHARS."""
    anchor = question["source_section"]
    ap = parse_section(anchor)
    if not ap:
        return collect_oracle_context(question, use_raw=use_raw)
    anchor_pt, anchor_ch, _ = ap

    involved_parts = {anchor_pt}
    if "chain" in question:
        for n in question["chain"].get("path", []):
            p = parse_section(n)
            if p: involved_parts.add(p[0])

    ordered: list[str] = []
    # 1. Anchor chapter
    for s in list_sections_in(anchor_pt, anchor_ch):
        if s not in ordered: ordered.append(s)
    # 2. Rest of anchor part
    for s in list_sections_in(anchor_pt):
        if s not in ordered: ordered.append(s)
    # 3. Other involved parts in document order
    for pt in sorted(involved_parts):
        if pt == anchor_pt: continue
        for s in list_sections_in(pt):
            if s not in ordered: ordered.append(s)

    parts, used = [], []
    total = 0
    truncated = False
    for sid in ordered:
        text = load_section_text(sid, use_raw=use_raw)
        if not text: continue
        if total + len(text) > MAX_CONTEXT_CHARS:
            remaining = MAX_CONTEXT_CHARS - total - 200
            if remaining > 1000:
                text = text[:remaining] + "\n[section truncated]"
                parts.append(f"## {sid}\n\n{text}")
                used.append(sid + "(trunc)")
                total += len(text)
            truncated = True
            break
        parts.append(f"## {sid}\n\n{text}")
        used.append(sid)
        total += len(text)

    context = "\n\n".join(parts)
    if truncated:
        context = "[NOTE: some later sections omitted due to context length limits]\n\n" + context
    return context, used


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ANSWER_PROMPT = """\
You are a ship-design verification assistant. Below are excerpts from the DNV ship \
classification rules. Read them, then answer the question using the applicable clauses.

## SOURCE DOCUMENTS

{context}

## QUESTION

{question}

## INSTRUCTIONS
Find the relevant clauses in the source documents and reason through them step by \
step. Cite the clause numbers you use (e.g., "Pt.3 Ch.6 Sec.7 [1.1.2]"). Answer \
with concrete numeric results where the question asks for them.
"""


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
    {{"fact": "<fact 1 verbatim>", "present": true|false, "evidence": "<short quote from response if present>"}},
    ...
  ],
  "facts_present": <int>,
  "facts_total": <int>,
  "is_correct": true|false,
  "rationale": "<one-sentence rationale>"
}}

Mark `is_correct: true` only if ALL required facts are present. If any required \
fact is missing or contradicted, `is_correct: false`.
"""


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def strip_to_json(raw: str) -> str:
    raw = raw.strip()
    if "<think>" in raw:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw


def judge_response(
    question: dict,
    response: str,
    client: LLMClient,
    judge_model: str,
    cache_key: str,
) -> dict:
    required = question.get("required_facts") or []
    if not required:
        return {"is_correct": False, "error": "no_required_facts"}

    grounding = question.get("annotator_grounding", {}) or {}
    expected = grounding.get("expected_derivation") or grounding.get("chain_summary") or ""

    req_str = "\n".join(f"- {f}" for f in required)
    prompt = JUDGE_PROMPT.format(
        question=question.get("question_text", ""),
        expected=expected,
        required_facts=req_str,
        response=response,
    )
    raw = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=judge_model, max_tokens=16384, temperature=0.0,
        cache_key=cache_key,
    )
    raw = strip_to_json(raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {"is_correct": False, "error": "judge_parse_fail", "raw": raw[:500]}
    try:
        return json.loads(raw[i:j + 1])
    except json.JSONDecodeError:
        return {"is_correct": False, "error": "judge_json_fail", "raw": raw[:500]}


def evaluate_question(
    q: dict,
    condition: str,
    client: LLMClient,
    model: str,
    judge_model: str,
    use_raw: bool,
    answer_client: LLMClient | None = None,
) -> dict:
    # Allow an alternate client for the answer call only (e.g. route
    # full_context through a provider with a bigger context window).
    # The judge always uses `client`.
    if answer_client is None:
        answer_client = client
    if condition == "oracle":
        context, used = collect_oracle_context(q, use_raw=use_raw)
    else:
        context, used = collect_full_context(q, use_raw=use_raw)

    prompt = ANSWER_PROMPT.format(context=context, question=q.get("question_text", ""))
    # Condition-aware max_tokens: full_context feeds up to ~600K chars (~150K
    # tokens) as input, and the upstream pool routes to various keys with
    # different total-request caps. Empirically, only max_tokens=1024 runs
    # reliably across every routing path for full_context; higher values
    # intermittently fail with 400 "context too long". Oracle contexts are
    # much smaller (typically <10K tokens), so we can afford a wider output.
    answer_max_tokens = 16384 if condition == "oracle" else 1024
    cache_key = f"text_v7_mt16k_{condition}_{q['id']}_{model}_{'raw' if use_raw else 'clean'}"
    try:
        response = answer_client.call_with_retry(
            messages=[{"role": "user", "content": prompt}],
            model=model, max_tokens=answer_max_tokens, temperature=0.0,
            cache_key=cache_key,
        )
    except Exception as e:
        response = f"[ERROR: {type(e).__name__}: {e}]"

    verdict = judge_response(
        q, response, client, judge_model,
        cache_key=f"judge_text_v7_mt16k_{condition}_{q['id']}_{model}_{judge_model}_{'raw' if use_raw else 'clean'}",
    )

    return {
        "id": q["id"],
        "tier": q["tier"],
        "tested_pattern": q.get("tested_pattern", "?"),
        "format": q.get("format", "?"),
        "confidence": q.get("confidence", "?"),
        "condition": condition,
        "sections_used": used,
        "context_chars": len(context),
        "response": response,
        "verdict": verdict,
        "is_correct": bool(verdict.get("is_correct", False)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", default="/workspace/regbench_pilot/pilot_49_v7_with_gold_pages.json")
    parser.add_argument("--out", default="/workspace/regbench_pilot/pilot_49_v7_text_results.json")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--judge", default="claude-sonnet-4-6")
    parser.add_argument("--conditions", default="oracle,full_context")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--raw", action="store_true", help="Use MinerU _raw.md instead of polished .md")
    parser.add_argument("--fc_api_key", default=None, help="Alt API key for full_context calls (e.g. cursoropenai)")
    parser.add_argument("--fc_base_url", default=None, help="Alt base URL for full_context calls (e.g. http://127.0.0.1:8901/v1)")
    args = parser.parse_args()

    questions = json.loads(Path(args.input).read_text())
    if args.limit:
        questions = questions[: args.limit]
    log(f"Loaded {len(questions)} questions from {args.input}")
    log(f"Using {'RAW OCR' if args.raw else 'polished markdown'} as context")

    client = LLMClient(
        cache_dir="/workspace/regbench_pilot/text_v7_cache",
        token_log_path="/workspace/regbench_pilot/text_v7_token_log.jsonl",
    )

    fc_client = None
    if args.fc_base_url:
        fc_client = LLMClient(
            cache_dir="/workspace/regbench_pilot/text_v7_cache",
            token_log_path="/workspace/regbench_pilot/text_v7_fc_token_log.jsonl",
            api_key=args.fc_api_key,
            base_url=args.fc_base_url,
        )
        log(f"Alt full_context client: base_url={args.fc_base_url}")

    results: list[dict] = []
    conditions = args.conditions.split(",")

    for cond in conditions:
        log(f"\n=== CONDITION: {cond} ===")
        answer_client = fc_client if (cond == "full_context" and fc_client) else client
        for i, q in enumerate(questions):
            r = evaluate_question(q, cond, client, args.model, args.judge, args.raw, answer_client=answer_client)
            results.append(r)
            mark = "✓" if r["is_correct"] else "✗"
            n_facts = r["verdict"].get("facts_present", "?")
            n_total = r["verdict"].get("facts_total", "?")
            log(f"  [{i+1}/{len(questions)}] {mark} T{q['tier']} {q['id']} "
                f"{r['context_chars']:,} chars  facts={n_facts}/{n_total}")

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))

    # Aggregate
    log(f"\n{'='*60}")
    log(f"RESULTS — {args.model} judged by {args.judge}")
    log(f"{'='*60}")

    for cond in conditions:
        log(f"\n--- {cond} ---")
        log(f"{'Tier':<6} {'correct':<12}")
        tiers_seen = sorted(set(r["tier"] for r in results))
        tot_c = tot_n = 0
        for tier in tiers_seen:
            c_results = [r for r in results if r["condition"] == cond and r["tier"] == tier]
            if not c_results: continue
            c = sum(1 for r in c_results if r["is_correct"])
            n = len(c_results)
            log(f"T{tier:<5} {c:>2}/{n:<2} ({100*c/n:4.1f}%)")
            tot_c += c
            tot_n += n
        log(f"TOTAL: {tot_c}/{tot_n} ({100*tot_c/max(tot_n,1):.1f}%)")

    log(f"\nToken usage: {client.get_token_usage()}")


if __name__ == "__main__":
    main()
