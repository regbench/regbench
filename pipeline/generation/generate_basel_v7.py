#!/usr/bin/env python3
"""
Basel III v7 question generator (bank capital-adequacy scenarios).

Ports /workspace/homerun/generate_pilot_v7.py to 12 CFR Part 217.

Structural changes vs DNV:
  - Section loader reads /workspace/regbench_basel/sections.json (Part 217 only)
  - Chain input sampled from /workspace/regbench_basel/xref_graph.json
  - 4-tier Basel taxonomy (T4 dropped: cross-part exits corpus)
  - Scenario domain: bank compliance (capital ratios, RWA, exposure classification)
  - Leak regex blocks §, 12 CFR, Subpart X, paragraph letters, ¶

Strict grading semantics (atomic required_facts) and self-confidence
filter (verified|dTx) carry over unchanged.
"""
import json, os, re, sys, argparse, random
from pathlib import Path
from datetime import datetime
from collections import Counter

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge

sys.path.insert(0, str(Path(__file__).parent))
from llm_client import LLMClient

SECTIONS_PATH = Path("/workspace/regbench_basel/sections.json")
GRAPH_PATH = Path("/workspace/regbench_basel/xref_graph.json")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- Corpus access ----------

_SECTIONS_CACHE = None


def load_sections() -> dict:
    global _SECTIONS_CACHE
    if _SECTIONS_CACHE is None:
        _SECTIONS_CACHE = json.loads(SECTIONS_PATH.read_text())
    return _SECTIONS_CACHE


def load_section_text(section_id: str) -> str:
    sections = load_sections()
    s = sections.get(section_id)
    if not s:
        return ""
    return s.get("text", "")


def section_head(section_id: str) -> str:
    sections = load_sections()
    s = sections.get(section_id)
    if not s:
        return section_id
    return f"{s.get('sec', section_id)} — {s.get('head', '')}"


# ---------- Prompt ----------

PROMPT_V7 = """\
You are constructing a bank-compliance verification question for the RegBench benchmark (Basel III sub-benchmark, US implementation: 12 CFR Part 217).

The question will be posed to a DIFFERENT AI model (the answerer) which will be given the actual source regulatory text. The answerer must figure out WHICH paragraphs apply from scratch. Your job is to write a realistic bank-compliance scenario — not a rule-lookup question.

## TIER {tier}: {tier_description}

## CHAIN (HIDDEN from the answerer — for your reasoning only)
{chain_description}

## SOURCE TEXTS (your reference material — the answerer will see the regulatory text)

### Start section: {start_section} ({start_head})
{start_text}

### End section: {end_section} ({end_head})
{end_text}

{intermediate_block}

## WRITING A GOOD QUESTION

**GOOD example (chain hidden):**
> "A Category II bank holding company reports common equity tier 1 capital of
>  $42 billion, additional tier 1 capital of $6 billion, and tier 2 capital of
>  $9 billion. Its standardized total risk-weighted assets are $520 billion.
>  The institution is subject to the countercyclical capital buffer, currently
>  set at 0.5% for US exposures (which constitute 80% of its credit RWA).
>  It holds $14 billion of certain non-significant investments in the capital
>  of unconsolidated financial institutions. Determine whether the institution
>  meets its minimum capital and buffer requirements, and compute the standardized
>  capital conservation buffer the institution must hold to avoid restrictions on
>  capital distributions."

This is GOOD because:
  - Concrete balance-sheet inputs (CET1, AT1, T2, RWA, exposure mix, investment holdings)
  - The answerer must figure out: "Category II" → §217.2 definition → which minimum ratios apply → buffer composition → threshold deduction rules
  - NO section numbers, NO paragraph letters, NO "under §217.X" phrasing

**BAD example (chain leaked):**
> "According to § 217.10(c), given the institution's standardized RWA and its
>  subpart B minimums, and considering § 217.22(d) threshold deductions, what
>  is the standardized capital conservation buffer per § 217.11(a)?"

This is BAD because:
  - Tells the model exactly which paragraphs to read
  - Reduces navigation to verbatim lookup
  - Does NOT test cross-reference reasoning

## STRICT RULES
1. question_text MUST NOT contain any of: "§", "Sec. 217", "section 217", "Part 217", "Subpart A/B/C/...", "paragraph (a)/(b)/(c)...", bracketed paragraph IDs like "(a)(1)(ii)", or phrases like "according to", "per § 217.X", "under paragraph", "12 CFR".
2. question_text MUST be a realistic bank-compliance scenario with concrete numeric parameters (dollar amounts, percentages, ratios, category labels) that the answerer can check against regulatory rules.
3. The question must genuinely REQUIRE the chain to answer correctly — if the answerer could solve it by reading only the start section, that's too easy.
4. Pick concrete parameters that EXERCISE the chain (values near thresholds, exposure types that trigger the cross-reference, institution categories that gate applicability).
5. required_facts is an explicit list of 3–7 atomic statements that the answer MUST contain to be correct. Each fact should be independently checkable. Examples: "CET1 ratio = 42/520 = 8.08%", "institution is a Category II advanced approaches institution", "countercyclical buffer = 0.40%", "standardized capital conservation buffer = 2.5% + 0.40% + 1.0% = 3.90%", "minimum CET1 ratio requirement is met".
6. Use bank-domain vocabulary: "Board-regulated institution", "common equity tier 1 (CET1)", "risk-weighted assets (RWA)", "standardized approach", "advanced approaches", "regulatory capital", "threshold deduction", "countercyclical buffer", "capital conservation buffer", "GSIB surcharge", "eligible guarantee", "credit conversion factor (CCF)", "supervisory delta", etc.
7. Do NOT quote full regulatory text verbatim in question_text. Rephrase into scenario inputs.

## OUTPUT (single JSON object, no fences)
{{
  "question_text": "<realistic bank-compliance scenario with concrete parameters, NO § or paragraph references>",
  "format": "explanation" or "mc4",
  "options": null or {{"A":"...","B":"...","C":"...","D":"..."}},
  "correct": null or "A|B|C|D",
  "annotator_grounding": {{
    "start_clause": "§ 217.10(a)",
    "end_clause": "§ 217.22(d)",
    "chain_summary": "Start at §217.10 minimum ratios, which references §217.22 threshold deduction treatment",
    "expected_derivation": "<step-by-step paragraph-by-paragraph reasoning the annotator used>"
  }},
  "required_facts": [
    "<atomic fact 1>",
    "<atomic fact 2>",
    ...
  ],
  "scenario_parameters": {{"key": "value"}},
  "confidence": "verified|dT{tier}",
  "tested_pattern": "edge_case|gating_condition|applicability|formula_branch|threshold_deduction|cross_section_lookup|cross_subpart_navigation|category_classification"
}}
"""


TIER_DESCRIPTIONS = {
    0: "atomic paragraph comprehension within a single subrule (threshold check, definition application)",
    1: "within-section reasoning (formula application, table lookup, or conditional logic inside one § paragraph block)",
    2: "cross-section reasoning within the same subpart (one § paragraph references another § in the same subpart)",
    3: "cross-subpart reasoning within Part 217 (one § paragraph references a § in a different subpart, e.g. Subpart B → Subpart D)",
}


# ---------- Leak check ----------

LEAK_PATTERNS = [
    (r"§\s*\d", "§ literal"),
    (r"\bSec(tion)?\.?\s*217\b", "Section 217"),
    (r"\bPart\s*217\b", "Part 217"),
    (r"12\s*CFR", "12 CFR"),
    (r"\bSubpart\s+[A-J]\b", "Subpart letter"),
    (r"\bparagraph\s*\(", "paragraph (X)"),
    (r"\(\s*[a-z]\s*\)\s*\(\s*\d", "(a)(1) id"),
    (r"\(\s*\d+\s*\)\s*\(\s*[ivx]+\s*\)", "(1)(i) id"),
    (r"\baccording\s+to\b", "according to"),
    (r"\bunder\s+§", "under §"),
    (r"\bas\s+defined\s+in", "as defined in"),
]


def leak_check(text: str) -> list[str]:
    leaked = []
    for pat, _label in LEAK_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            leaked.append(m.group(0))
    return leaked


# ---------- Chain sampling ----------

def sample_chains(graph: dict, target_per_tier: int, seed: int = 42) -> list[dict]:
    """Sample a balanced set of chains across tiers 2/3 (multi-section) and
    synthesize T0/T1 'chains' from single sections."""
    rng = random.Random(seed)
    all_chains = graph["chains"]

    # Multi-section chains: group by their stored 'tier'
    by_tier = {2: [], 3: []}
    for c in all_chains:
        t = c.get("tier")
        if t in by_tier:
            by_tier[t].append(c)

    # Deduplicate by (start,end) pair — take shallowest path per pair
    def dedup(chains):
        seen = {}
        for c in sorted(chains, key=lambda x: x.get("depth", 99)):
            key = (c["start"], c["end"])
            if key not in seen:
                seen[key] = c
        return list(seen.values())

    sampled = []
    for tier in [2, 3]:
        pool = dedup(by_tier[tier])
        rng.shuffle(pool)
        n = min(target_per_tier, len(pool))
        for c in pool[:n]:
            c2 = dict(c)
            c2["_assigned_tier"] = tier
            sampled.append(c2)

    # T0/T1: single-section pseudo-chains from Part 217 sections
    sections = list(graph["sections"].keys())
    rng.shuffle(sections)
    for tier in [0, 1]:
        n = min(target_per_tier, len(sections))
        for sec in sections[:n]:
            sampled.append({
                "start": sec,
                "end": sec,
                "path": [sec],
                "depth": 0,
                "tier": tier,
                "_assigned_tier": tier,
                "kinds": ["within_section"],
                "raws": [],
            })
        rng.shuffle(sections)

    return sampled


# ---------- Generation ----------

def truncate(text: str, max_chars: int = 10000) -> str:
    if len(text) <= max_chars:
        return text
    end = int(max_chars * 0.3)
    start = max_chars - end - 50
    return text[:start] + "\n\n[...truncated...]\n\n" + text[-end:]


def build_chain_description(c: dict) -> str:
    path = c.get("path", [])
    depth = c.get("depth", 0)
    raws = c.get("raws", [])
    lines = [f"Chain depth: {depth}", f"Path: {' → '.join(path)}"]
    if raws:
        lines.append("Raw xref spans:")
        for r in raws:
            lines.append(f"  - {r}")
    return "\n".join(lines)


def generate_one(c: dict, client: LLMClient, model: str, qid: str) -> dict | None:
    tier = c["_assigned_tier"]
    start_sec = c["start"]
    end_sec = c["end"]
    start_text = load_section_text(start_sec)
    if not start_text:
        return None
    end_text = load_section_text(end_sec) if end_sec != start_sec else ""

    intermediate_block = ""
    path = c.get("path", [])
    for node in path[1:-1]:
        mid = load_section_text(node)
        if mid:
            intermediate_block += f"\n### Intermediate section: {node} ({section_head(node)})\n{truncate(mid, 4000)}\n"

    prompt = PROMPT_V7.format(
        tier=tier,
        tier_description=TIER_DESCRIPTIONS[tier],
        chain_description=build_chain_description(c),
        start_section=start_sec,
        start_head=section_head(start_sec),
        end_section=end_sec,
        end_head=section_head(end_sec),
        start_text=truncate(start_text, 8000),
        end_text=truncate(end_text, 8000) if end_text else "(same as start section)",
        intermediate_block=intermediate_block,
    )

    cache_key = f"basel_v7_{qid}"
    response = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=model, max_tokens=2048, temperature=0.3,
        cache_key=cache_key,
    )
    return parse_response(response, c, qid, tier)


def parse_response(response: str, c: dict, qid: str, tier: int) -> dict | None:
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
    if not q.get("question_text"):
        return None

    leaked = leak_check(q["question_text"])
    return {
        "id": qid,
        "domain": "basel_12cfr217",
        "tier": tier,
        "source_section": c["start"],
        "question_text": q["question_text"],
        "format": q.get("format", "explanation"),
        "options": q.get("options"),
        "correct": q.get("correct"),
        "annotator_grounding": q.get("annotator_grounding", {}),
        "required_facts": q.get("required_facts", []),
        "scenario_parameters": q.get("scenario_parameters", {}),
        "confidence": q.get("confidence", f"dT{tier}"),
        "tested_pattern": q.get("tested_pattern", "?"),
        "generation_method": "v7_chain_hidden",
        "leak_check": {"passed": len(leaked) == 0, "leaked_tokens": leaked},
        "chain": {
            "start": c["start"],
            "end": c["end"],
            "path": c.get("path", []),
            "depth": c.get("depth", 0),
            "kinds": c.get("kinds", []),
            "raws": c.get("raws", []),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/workspace/regbench_basel/basel_v7_candidates.json")
    parser.add_argument("--per-tier", type=int, default=75, help="Candidates per tier (T0/T1/T2/T3)")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--smoke", type=int, default=0, help="If >0, generate only N candidates (1/tier if N>=4) for prompt sanity-check")
    args = parser.parse_args()

    graph = json.loads(GRAPH_PATH.read_text())
    sampled = sample_chains(graph, args.per_tier, seed=args.seed)

    if args.smoke > 0:
        # Keep 1 per tier up to smoke count
        by_tier = {0: [], 1: [], 2: [], 3: []}
        for c in sampled:
            by_tier[c["_assigned_tier"]].append(c)
        kept = []
        per = max(1, args.smoke // 4)
        for tier in [0, 1, 2, 3]:
            kept.extend(by_tier[tier][:per])
        sampled = kept[:args.smoke]
        log(f"SMOKE TEST: generating {len(sampled)} candidates ({per}/tier)")
    else:
        log(f"Generating {len(sampled)} candidates ({args.per_tier}/tier × 4 tiers)")

    client = LLMClient(
        cache_dir="/workspace/regbench_basel/qgen_cache",
        token_log_path="/workspace/regbench_basel/qgen_token_log.jsonl",
    )

    results = []
    for i, c in enumerate(sampled):
        tier = c["_assigned_tier"]
        qid = f"B_T{tier}_{i:04d}"
        log(f"  [{i+1}/{len(sampled)}] T{tier} {qid} ({c['start']} → {c['end']})")
        q = generate_one(c, client, args.model, qid)
        if q:
            results.append(q)
            leak = "⚠ LEAK" if not q["leak_check"]["passed"] else "✓"
            conf = q.get("confidence", "?")
            log(f"    {leak} conf={conf} pattern={q.get('tested_pattern','?')}")
            if not q["leak_check"]["passed"]:
                log(f"       leaked: {q['leak_check']['leaked_tokens']}")
        else:
            log("    ✗ failed to parse")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    leaked = sum(1 for r in results if not r["leak_check"]["passed"])
    verified = sum(1 for r in results if r.get("confidence") == "verified")
    tier_counts = Counter(r["tier"] for r in results)
    log(f"\nSaved {len(results)} → {out_path}")
    log(f"  Leak: {len(results)-leaked} clean, {leaked} leaked")
    log(f"  Confidence: {verified} verified, {len(results)-verified} dTx")
    log(f"  Tier counts: {dict(tier_counts)}")
    log(f"  Tokens: {client.get_token_usage() if hasattr(client, 'get_token_usage') else 'n/a'}")


if __name__ == "__main__":
    main()
