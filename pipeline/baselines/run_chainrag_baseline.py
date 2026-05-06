#!/usr/bin/env python3
"""
ChainRAG: BM25 anchors + regex-follow 1-hop cross-references.

Pipeline:
1. BM25 retrieve top-K anchor sections (K=5 default).
2. Scan retrieved text with the 3 cross-reference regex patterns.
3. Fetch the 1-hop referenced sections (deduplicated, bounded).
4. Concatenate anchor + referenced sections → context.
5. LLM answers.

Direct comparison baseline to BM25 and full_context. Tests whether explicit
regex-based chain following (text-faithful, structural) beats graph-summary RAG.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge
sys.path.insert(0, str(Path(__file__).parent))

from run_text_baseline import ANSWER_PROMPT, JUDGE_PROMPT, strip_to_json
from run_bm25_baseline import (
    discover_all_sections, build_bm25_index, retrieve_topk,
    load_section_text, parse_section,
)
from llm_client import LLMClient


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# Same 3 regex patterns used to build the xref graph (build_xref_graph.py)
CROSS_REF_PATTERNS = [
    # Full xref: "Pt.3 Ch.6 Sec.7 [1.1.2]"
    (re.compile(r'Pt\.?\s*(\d+)\s+Ch\.?\s*(\d+)\s+Sec\.?\s*(\d+)'), 'full'),
    # Within-part xref: "Ch.6 Sec.7"
    (re.compile(r'(?<!Pt\.\s)(?<!Pt\. )Ch\.?\s*(\d+)\s+Sec\.?\s*(\d+)'), 'within_part'),
]


def extract_cross_refs(text: str, anchor_section: str) -> list[str]:
    """Return list of referenced section IDs found in `text`.

    anchor_section is used to resolve within-part references.
    """
    refs = set()
    anchor = parse_section(anchor_section)
    anchor_pt = anchor[0] if anchor else None

    # Pattern 1: Pt.X Ch.Y Sec.Z
    for m in CROSS_REF_PATTERNS[0][0].finditer(text):
        pt, ch, sec = m.group(1), m.group(2), m.group(3)
        refs.add(f"Pt{pt}.Ch{ch}.Sec{sec}")

    # Pattern 2: Ch.Y Sec.Z (needs anchor_pt to resolve)
    if anchor_pt is not None:
        # Use a stripped copy to find Ch Sec without Pt prefix
        scan_text = re.sub(r'Pt\.?\s*\d+\s+', '__MASKED__ ', text)
        for m in CROSS_REF_PATTERNS[1][0].finditer(scan_text):
            ch, sec = m.group(1), m.group(2)
            refs.add(f"Pt{anchor_pt}.Ch{ch}.Sec{sec}")

    # Pattern 3: local Sec.Y — reference to same chapter as anchor
    anchor_ch = anchor[1] if anchor else None
    if anchor_pt and anchor_ch:
        for m in re.finditer(r'Sec\.?\s*(\d+)\s*\[', text):
            # Skip if preceded by "Ch." in the close neighborhood
            pre = text[max(0, m.start()-15):m.start()]
            if re.search(r'Ch\.?\s*\d+\s*$', pre):
                continue
            sec = m.group(1)
            refs.add(f"Pt{anchor_pt}.Ch{anchor_ch}.Sec{sec}")

    refs.discard(anchor_section)  # don't re-add self
    return sorted(refs)


def build_chainrag_context(
    bm25, sections, texts, q_text: str,
    top_k: int = 5, max_hop1: int = 10, max_chars: int = 400_000,
) -> tuple[str, dict]:
    """Return (context_str, stats) for a question."""
    # Step 1: BM25 anchors
    anchors = retrieve_topk(bm25, sections, texts, q_text, k=top_k)
    anchor_ids = [a[0] for a in anchors]
    anchor_texts = {a[0]: a[1] for a in anchors}

    # Step 2+3: regex scan + 1-hop fetch
    hop1_ids = set()
    for anchor_id, anchor_text in zip(anchor_ids, [a[1] for a in anchors]):
        refs = extract_cross_refs(anchor_text, anchor_id)
        for r in refs:
            if r in anchor_ids: continue  # already in anchors
            if r in hop1_ids: continue
            hop1_ids.add(r)
            if len(hop1_ids) >= max_hop1:
                break
        if len(hop1_ids) >= max_hop1:
            break

    # Step 4: build context in document order
    all_sections = list(anchor_ids)
    for h in sorted(hop1_ids, key=lambda s: (parse_section(s) or (99,99,99))):
        if h not in all_sections:
            all_sections.append(h)

    parts = []
    used = []
    total = 0
    for sid in all_sections:
        text = anchor_texts.get(sid) or load_section_text(sid)
        if not text:
            continue
        if total + len(text) > max_chars:
            # cap remainder
            remain = max_chars - total - 200
            if remain > 2000:
                text = text[:remain] + "\n[truncated]"
                parts.append(f"## {sid}\n\n{text}")
                used.append(sid + "(trunc)")
                total += len(text)
            break
        parts.append(f"## {sid}\n\n{text}")
        used.append(sid)
        total += len(text)

    stats = {
        "anchors": anchor_ids,
        "hop1_added": sorted(hop1_ids),
        "sections_used": used,
        "context_chars": total,
    }
    return "\n\n".join(parts), stats


def judge_response(q, response, client, judge_model, cache_key):
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
        return json.loads(raw[i:j+1])
    except json.JSONDecodeError:
        return {"is_correct": False, "error": "judge_json_fail", "raw": raw[:500]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", default="/workspace/regbench_pilot/pilot_49_v7_with_gold_pages.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--judge", default="claude-sonnet-4-6")
    parser.add_argument("--top-k", type=int, default=5, help="BM25 anchor top-K")
    parser.add_argument("--max-hop1", type=int, default=10, help="Max 1-hop referenced sections")
    parser.add_argument("--max-chars", type=int, default=400_000, help="Max context chars")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--answer_api_key", default=None, help="Alt API key for answer calls")
    parser.add_argument("--answer_base_url", default=None, help="Alt base URL for answer calls")
    args = parser.parse_args()

    questions = json.loads(Path(args.input).read_text())
    if args.limit:
        questions = questions[:args.limit]
    log(f"Loaded {len(questions)} questions")
    log(f"ChainRAG: top_k={args.top_k}, max_hop1={args.max_hop1}, max_chars={args.max_chars}")
    log(f"Answer model: {args.model}, Judge: {args.judge}")

    log("Building BM25 index...")
    sections = discover_all_sections()
    bm25, valid_sections, texts = build_bm25_index(sections)
    log(f"  Indexed {len(valid_sections)} sections")

    # Default client (judge + answer)
    client = LLMClient(
        cache_dir="/workspace/regbench_pilot/chainrag_cache",
        token_log_path="/workspace/regbench_pilot/chainrag_token_log.jsonl",
    )
    # Optional alt client for answer step only
    answer_client = client
    if args.answer_base_url:
        answer_client = LLMClient(
            cache_dir="/workspace/regbench_pilot/chainrag_cache",
            token_log_path="/workspace/regbench_pilot/chainrag_answer_token_log.jsonl",
            api_key=args.answer_api_key,
            base_url=args.answer_base_url,
        )
        log(f"Alt answer client: base_url={args.answer_base_url}")

    results = []
    for i, q in enumerate(questions):
        context, stats = build_chainrag_context(
            bm25, valid_sections, texts, q["question_text"],
            top_k=args.top_k, max_hop1=args.max_hop1, max_chars=args.max_chars,
        )
        prompt = ANSWER_PROMPT.format(context=context, question=q.get("question_text", ""))
        cache_key = f"chainrag_{args.model}_k{args.top_k}_h{args.max_hop1}_{q['id']}".replace(".", "_")
        try:
            response = answer_client.call_with_retry(
                messages=[{"role": "user", "content": prompt}],
                model=args.model, max_tokens=16384, temperature=0.0,
                cache_key=cache_key,
            )
        except Exception as e:
            response = f"[ERROR: {type(e).__name__}: {e}]"

        verdict = judge_response(
            q, response, client, args.judge,
            cache_key=f"judge_chainrag_{args.model}_{q['id']}_{args.judge}".replace(".", "_"),
        )

        r = {
            "id": q["id"],
            "tier": q["tier"],
            "condition": f"chainrag_k{args.top_k}_h{args.max_hop1}",
            "question_text": q.get("question_text", ""),
            "required_facts": q.get("required_facts", []),
            "annotator_grounding": q.get("annotator_grounding", {}),
            "response": response,
            "verdict": verdict,
            "is_correct": bool(verdict.get("is_correct", False)),
            **stats,
        }
        results.append(r)
        mark = "✓" if r["is_correct"] else "✗"
        fp = verdict.get("facts_present", "?")
        ft = verdict.get("facts_total", "?")
        log(f"  [{i+1}/{len(questions)}] {mark} T{q['tier']} {q['id']}  "
            f"anchors={len(stats['anchors'])} hop1={len(stats['hop1_added'])} "
            f"chars={stats['context_chars']:,} facts={fp}/{ft}")
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))

    # Summary
    from collections import Counter
    log(f"\n{'='*60}")
    log(f"ChainRAG {args.model} — top_k={args.top_k} hop1_max={args.max_hop1}")
    log(f"{'='*60}")
    c = sum(1 for r in results if r['is_correct'])
    fp = sum((r['verdict'] or {}).get('facts_present', 0) or 0 for r in results)
    ft = sum((r['verdict'] or {}).get('facts_total', 0) or 0 for r in results)
    log(f"Overall: {c}/{len(results)} ({100*c/max(len(results),1):.1f}%), fact rate {100*fp/max(ft,1):.1f}%")
    by_tier = Counter(); corr = Counter()
    for r in results:
        by_tier[r['tier']] += 1
        if r['is_correct']: corr[r['tier']] += 1
    for t in sorted(by_tier):
        log(f"  T{t}: {corr[t]}/{by_tier[t]} ({100*corr[t]/by_tier[t]:.0f}%)")


if __name__ == "__main__":
    main()
