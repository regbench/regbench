#!/usr/bin/env python3
"""
Generate 500 T0-T4 questions (v6 format) for RegBench scale-up.

100 questions per tier. T0/T1 from individual sections (round-robin with
variants for repeat visits). T2-T4 from cross-reference chains.
"""
import json, os, re, sys, argparse, random
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge

sys.path.insert(0, str(Path(__file__).parent))
from llm_client import LLMClient

PT5_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt5")
PT3_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt3")
PT1_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt1")

XREF_GRAPH = Path("/workspace/xref_graph.json")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def section_to_dir(section_id: str) -> Path | None:
    """Convert 'Pt5.Ch2.Sec10' → directory path."""
    m = re.match(r'Pt(\d+)\.Ch(\d+)\.Sec(\d+)', section_id)
    if not m:
        return None
    pt, ch, sec = m.group(1), m.group(2), m.group(3)
    base_map = {"1": PT1_DIR, "3": PT3_DIR, "5": PT5_DIR}
    base = base_map.get(pt)
    if not base:
        return None
    return base / f"DNV-RU-Pt{pt}-Chap{ch}-sec{sec}" / "pipeline"


def load_section_text(section_id: str) -> str:
    pipeline = section_to_dir(section_id)
    if not pipeline or not pipeline.exists():
        return ""
    for f in pipeline.glob("*.md"):
        if "_raw" not in f.name and "_content" not in f.name:
            return f.read_text(errors="replace")
    return ""


def section_to_anchor(section_id: str) -> str:
    """Convert 'Pt5.Ch2.Sec10' → 'pt5-ch2-sec10' (annotator format)."""
    m = re.match(r'Pt(\d+)\.Ch(\d+)\.Sec(\d+)', section_id)
    if not m:
        return section_id.lower()
    return f"pt{m.group(1)}-ch{m.group(2)}-sec{m.group(3)}"


# =============================================================================
# Tier-specific source selection
# =============================================================================

def select_t0_t1_sources(n_per_tier: int) -> list[dict]:
    """For T0/T1: pick sections from all 3 parts, round-robin with variant
    index so repeated visits to the same section generate different clauses."""
    candidates = []
    for base in [PT5_DIR, PT3_DIR, PT1_DIR]:
        for sec_dir in sorted(base.iterdir()):
            m = re.match(r'DNV-RU-Pt(\d+)-Chap(\d+)-sec(\d+)', sec_dir.name)
            if m:
                candidates.append(f"Pt{m.group(1)}.Ch{m.group(2)}.Sec{m.group(3)}")

    items = []
    for tier in [0, 1]:
        shuffled = list(candidates)
        random.seed(42 + tier)
        random.shuffle(shuffled)
        for i in range(n_per_tier):
            sec = shuffled[i % len(shuffled)]
            variant = i // len(shuffled)
            items.append({"tier": tier, "section": sec, "variant": variant})
    return items


def select_chain_sources(graph: dict, tier: int, n: int) -> list[dict]:
    """For T2/T3/T4: pick mechanically-extracted chains from the graph."""
    chains = [c for c in graph["chains"] if c.get("tier") == tier]
    if not chains:
        return []

    # Deduplicate by (start, end)
    seen_pairs = set()
    unique = []
    for c in chains:
        key = (c["start"], c["end"], len(c["edges"]))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        unique.append(c)

    # Sort by depth (prefer shorter chains), break ties by diversity of start/end
    unique.sort(key=lambda c: (c["depth"], c["start"], c["end"]))

    random.seed(42 + tier)
    pool_size = min(len(unique), max(n * 3, 500))
    sample = unique[:n] if len(unique) <= n else random.sample(unique[:pool_size], n)

    return [{"tier": tier, "chain": c} for c in sample]


# =============================================================================
# Prompts (annotator-aligned)
# =============================================================================

T0_T1_PROMPT = """\
You are constructing a SHIP DESIGN VERIFICATION question for the RegBench benchmark.

## TIER {tier} TASK
Tier {tier} questions test {tier_description}.
The question MUST anchor to a specific clause in this section. The model will
have the FULL section text but must find and apply the right clause.

## SECTION TO TEST: {anchor}
{section_text}

## ANNOTATOR'S GOLD-STANDARD FORMAT (study this pattern)

> "According to pt5-ch2-sec10 [1.1.1], if a container ship's upper deck plate has thickness 105mm using steel grade NV47, does this section's regulation apply? Why?"
> Expected answer: "No. For plates exceeding 100mm, application must be agreed with the Society on a case-by-case basis."

Key elements:
- Anchor at a specific clause in the question text (e.g., "pt5-ch2-sec10 [2.1.4]")
- Concrete numeric parameters (thickness, steel grade, position)
- Asks about a SHIP DESIGN PROPERTY (compliance, required value, applicability)
- "Why?" prompt for explanation questions
- Tests the rule's APPLICATION, not its wording

## RULES
1. Pick ONE specific clause from the section above
2. Frame the question with concrete ship parameters that exercise that clause
3. The answer should require READING and APPLYING the clause, not just quoting it
4. For threshold/range clauses, pick parameters near the boundary (edge case)
5. For tables, pick parameters that determine a specific row+column
6. For formulas, pick parameters that produce a verifiable numeric result
7. Default to dTx confidence (default dT{tier}). Only mark verified if the answer
   is a literal direct quote with no interpretation.

## OUTPUT (single JSON object, no fences)
{{
  "question": "According to {anchor} [X.Y.Z], <scenario with concrete parameters>. <design verification question>? Why?",
  "format": "explanation | mc4",
  "options": null OR {{"A":"...","B":"...","C":"...","D":"..."}},
  "correct": null OR "A|B|C|D",
  "expected_reasoning": "<the answer in 1-3 sentences with derivation>",
  "evidence_text": "<literal quote from the clause>",
  "confidence": "verified|dT{tier}",
  "anchor_clause": "{anchor} [X.Y.Z]",
  "tested_pattern": "edge_case|gating_condition|application_perspective|formula_branch_selection|cross_section_lookup|cross_volume_lookup|multi_hop_navigation",
  "design_parameters": {{"key": "value"}}
}}
"""


CHAIN_PROMPT = """\
You are constructing a CROSS-REFERENCE REASONING question for the RegBench benchmark.

## TIER {tier} TASK ({tier_description})

This is a CHAIN question. The model will be given the FULL source documents and
must navigate the cross-reference chain to answer correctly.

## CHAIN (mechanical)
Start: {start_section}
Path: {chain_str}
End: {end_section}

## START SECTION TEXT
{start_text}

## END SECTION TEXT
{end_text}

{intermediate_block}

## ANNOTATOR'S GOLD-STANDARD FORMAT

> "According to pt5-ch2-sec4 [2.3], when calculating shear stress at position x under a Hogging condition for a Buckling assessment, if x is less than half the ship length, what is Q_SW?"

Key elements:
- Anchor at the START clause (so the model knows where to begin)
- The question gives concrete engineering inputs (loading condition, position, assessment type)
- The model must internally navigate the chain to the END clause to find the answer
- Tests UNDERSTANDING of what tables/formulas are USED FOR, not what they contain
- The chain navigation is HIDDEN — the question doesn't say "see Sec.X then Sec.Y"

## RULES
1. Anchor the question at the start clause (e.g., "according to pt5-ch2-sec10 [2.1.4]")
2. DO NOT mention the end section in the question text — the model must find it
3. Concrete ship parameters that EXERCISE the chain (different values would give different answers)
4. The answer must require BOTH the start and end clauses
5. For T4 (cross-volume), the chain crosses Pt boundaries
6. Default to dT{tier} confidence

## OUTPUT (single JSON object, no fences)
{{
  "question": "According to {start_anchor} [clause], <scenario with concrete parameters>. <design verification question>?",
  "format": "explanation | mc4",
  "options": null OR {{"A":"...","B":"...","C":"...","D":"..."}},
  "correct": null OR "A|B|C|D",
  "expected_reasoning": "<step-by-step navigation: start clause → end clause → answer>",
  "evidence_text": "<literal quotes from BOTH clauses>",
  "confidence": "verified|dT{tier}",
  "anchor_clause": "{start_anchor} [clause]",
  "tested_pattern": "edge_case|gating_condition|application_perspective|formula_branch_selection|cross_section_lookup|cross_volume_lookup|multi_hop_navigation",
  "design_parameters": {{"key": "value"}}
}}
"""


TIER_DESCRIPTIONS = {
    0: "atomic clause comprehension within a single subrule (single value lookup, threshold check, or definition application)",
    1: "single-section reasoning (formula application, table lookup, or conditional logic within one section)",
    2: "cross-section reasoning within the same chapter (one section's clause references another section in the same chapter)",
    3: "cross-chapter reasoning within the same part (one section's clause references a clause in a different chapter of the same part)",
    4: "cross-volume reasoning (a section in Pt.5 references a clause in Pt.3 or Pt.1)",
}


def truncate(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text
    end = int(max_chars * 0.3)
    start = max_chars - end - 50
    return text[:start] + "\n\n[...truncated...]\n\n" + text[-end:]


def generate_t0_t1_question(item: dict, client: LLMClient, model: str) -> dict | None:
    section = item["section"]
    tier = item["tier"]
    text = load_section_text(section)
    if not text:
        return None
    text = truncate(text, 10000)
    anchor = section_to_anchor(section)

    variant = item.get("variant", 0)
    variant_hint = ""
    if variant > 0:
        variant_hint = (f"\n\n## IMPORTANT: This is visit #{variant+1} to this section. "
                        f"Pick a DIFFERENT clause than you would normally choose. "
                        f"Target a clause that tests a different rule aspect "
                        f"(e.g., a different table, formula, threshold, or applicability condition).\n")

    prompt = T0_T1_PROMPT.format(
        tier=tier,
        tier_description=TIER_DESCRIPTIONS[tier],
        anchor=anchor,
        section_text=text,
    ) + variant_hint

    cache_key = f"v500_t{tier}_{section}_var{variant}".replace(".", "_")
    cache_key = re.sub(r'[^a-zA-Z0-9_]', '_', cache_key)

    response = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=model, max_tokens=2048, temperature=0.3,
        cache_key=cache_key,
    )
    return parse_question_response(response, tier=tier, section=section)


def generate_chain_question(item: dict, client: LLMClient, model: str) -> dict | None:
    chain = item["chain"]
    tier = item["tier"]

    start = chain["start"]
    end = chain["end"]
    edges = chain["edges"]

    start_text = load_section_text(start)
    end_text = load_section_text(end)
    if not start_text or not end_text:
        return None

    # Intermediate texts
    intermediate_parts = []
    if len(chain["path"]) > 2:
        for node in chain["path"][1:-1]:
            mid = load_section_text(node)
            if mid:
                intermediate_parts.append(f"### Intermediate: {node}\n{truncate(mid, 4000)}")
    intermediate_block = "\n\n".join(intermediate_parts) if intermediate_parts else "(direct chain, no intermediate)"

    chain_str = " → ".join(e.get("raw", "?") for e in edges)
    start_anchor = section_to_anchor(start)

    prompt = CHAIN_PROMPT.format(
        tier=tier,
        tier_description=TIER_DESCRIPTIONS[tier],
        start_section=start,
        end_section=end,
        chain_str=chain_str,
        start_text=truncate(start_text, 8000),
        end_text=truncate(end_text, 8000),
        intermediate_block=truncate(intermediate_block, 4000),
        start_anchor=start_anchor,
    )

    cache_key = f"v500_t{tier}_{start}_{end}_{len(edges)}".replace(".", "_")
    cache_key = re.sub(r'[^a-zA-Z0-9_]', '_', cache_key)

    response = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=model, max_tokens=2048, temperature=0.3,
        cache_key=cache_key,
    )
    q = parse_question_response(response, tier=tier, section=start)
    if q:
        q["chain"] = {
            "start": start,
            "end": end,
            "path": chain["path"],
            "depth": chain["depth"],
            "edges_raw": [e.get("raw", "") for e in edges],
        }
    return q


def parse_question_response(response: str, tier: int, section: str) -> dict | None:
    response = response.strip()
    if "<think>" in response:
        response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```$", "", response)

    start_idx = response.find("{")
    end_idx = response.rfind("}")
    if start_idx < 0 or end_idx <= start_idx:
        return None
    try:
        q = json.loads(response[start_idx:end_idx + 1])
    except json.JSONDecodeError:
        return None

    if not q.get("question"):
        return None

    q["tier"] = tier
    q["source_section"] = section
    q["generation_method"] = "annotator_aligned_v6"
    q.setdefault("confidence", f"dT{tier}")
    q["review"] = {"question_clear": None, "answer_correct": None, "tier_correct": None}
    return q


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/workspace/regbench_500/regbench_500_v6.json")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--per-tier", type=int, default=100)
    args = parser.parse_args()

    log(f"Loading cross-reference graph...")
    graph = json.loads(XREF_GRAPH.read_text())
    log(f"  {graph['stats']['total_chains']} chains, by tier: {graph['stats']['chains_by_tier']}")

    client = LLMClient(
        cache_dir="/workspace/regbench_500/qgen_v6_cache",
        token_log_path="/workspace/regbench_500/qgen_v6_token_log.jsonl",
    )

    all_questions = []

    # T0 and T1 from individual sections
    log(f"\nSelecting T0/T1 sources...")
    t0_t1_items = select_t0_t1_sources(args.per_tier)
    for i, item in enumerate(t0_t1_items):
        log(f"  T{item['tier']} [{i+1}/{len(t0_t1_items)}] {item['section']}")
        q = generate_t0_t1_question(item, client, args.model)
        if q:
            all_questions.append(q)
            log(f"    ✓ {q.get('tested_pattern','?')} [{q.get('confidence','?')}]")
        else:
            log(f"    ✗ failed")

    # T2, T3, T4 from chains
    for tier in [2, 3, 4]:
        log(f"\nSelecting T{tier} chains...")
        items = select_chain_sources(graph, tier, args.per_tier)
        log(f"  Selected {len(items)} chains")
        for i, item in enumerate(items):
            chain = item["chain"]
            log(f"  T{tier} [{i+1}/{len(items)}] {chain['start']} → {chain['end']} (depth {chain['depth']})")
            q = generate_chain_question(item, client, args.model)
            if q:
                all_questions.append(q)
                log(f"    ✓ {q.get('tested_pattern','?')} [{q.get('confidence','?')}]")
            else:
                log(f"    ✗ failed")

    # Assign IDs
    for i, q in enumerate(all_questions):
        q["id"] = f"R500_{i+1:04d}"

    # Save
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_questions, indent=2, ensure_ascii=False))

    # Summary
    tier_counts = Counter(q["tier"] for q in all_questions)
    pattern_counts = Counter(q.get("tested_pattern", "?") for q in all_questions)
    conf_counts = Counter(q.get("confidence", "?") for q in all_questions)
    fmt_counts = Counter(q.get("format", "?") for q in all_questions)

    log(f"\nSaved {len(all_questions)} questions to {out_path}")
    log(f"  By tier: {dict(sorted(tier_counts.items()))}")
    log(f"  By pattern: {dict(pattern_counts)}")
    log(f"  By confidence: {dict(conf_counts)}")
    log(f"  By format: {dict(fmt_counts)}")
    log(f"  Token usage: {client.get_token_usage()}")


if __name__ == "__main__":
    main()
