#!/usr/bin/env python3
"""
RegBench Chain-Based Question Generator (v4 — Mechanical)
==========================================================
The Texas Sharpshooter approach:
  1. MECHANICALLY extract cross-reference chains from the document graph
  2. For each chain, compact the relevant paragraphs into a short passage
  3. Use LLM ONLY to generate concrete parameter values and format the MC options
  4. The chain structure, answer derivation, and tier are ALL mechanical

The model being tested gets the FULL LONG DOCUMENT and must find+follow the chain.

Key difference from v1-v3: the LLM doesn't choose what to ask about.
The chain determines the question. The LLM only fills in the parameters.
"""
from __future__ import annotations
import json, os, re, sys, argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge

sys.path.insert(0, str(Path(__file__).parent))
from llm_client import LLMClient


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_xref_graph(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def load_section_text(sections_dir: Path, section_id: str) -> str:
    """Load raw regulatory text for a section.
    section_id like 'Pt5.Ch2.Sec3' → DNV-RU-Pt5-Chap2-sec3"""
    m = re.match(r'Pt(\d+)\.Ch(\d+)\.Sec(\d+)', section_id)
    if not m:
        return ""
    pt, ch, sec = m.group(1), m.group(2), m.group(3)

    # Try multiple base dirs
    for base in [
        sections_dir / f"DNV-RU-SHIP-Pt{pt}" / f"DNV-RU-Pt{pt}-Chap{ch}-sec{sec}" / "pipeline",
    ]:
        if not base.exists():
            continue
        for f in sorted(base.glob("*.md")):
            if "_raw" not in f.name and "_content" not in f.name:
                return f.read_text(errors="replace")
    return ""


def extract_clause_text(full_text: str, clause: str, window: int = 1000) -> str:
    """Extract text around a specific clause reference in the document."""
    # Try exact bracket match first
    pattern = re.escape(f"[{clause}]")
    m = re.search(pattern, full_text)
    if not m:
        # Try section header style
        m = re.search(re.escape(clause), full_text)
    if not m:
        return ""
    start = max(0, m.start() - window // 2)
    end = min(len(full_text), m.end() + window // 2)
    return full_text[start:end]


def classify_chain(chain: dict) -> dict:
    """Classify a chain along the separated annotation axes."""
    depth = chain["depth"]
    cross_part = chain.get("cross_part", False)

    # Axis 1: Structural span
    if cross_part:
        span = "cross_doc"
    elif depth >= 1:
        span = "intra_doc"
    else:
        span = "local"

    # Axis 2: Hop count
    hop_count = depth

    # Legacy tier (for backwards compat)
    if cross_part:
        legacy_tier = 4
    elif depth >= 2:
        legacy_tier = 3
    elif depth == 1:
        legacy_tier = 2
    else:
        legacy_tier = 1

    return {
        "span": span,
        "hop_count": hop_count,
        "legacy_tier": legacy_tier,
    }


PARAMETER_PROMPT = """\
You are constructing a SHIP DESIGN VERIFICATION question for the RegBench benchmark.

## WHAT WE TEST
Whether an AI can verify a CONCRETE SHIP DESIGN against DNV regulations. The
scenario is always: a naval architect has specific design parameters (plate
thickness, frame spacing, breadth, material grade, loading condition) and needs
to determine if the design is compliant or what value is required.

## CROSS-REFERENCE CHAIN (mechanical — do not change)

Start clause: {start_clause}
Chain path: {chain_path}
End clause: {end_clause}

## CLAUSE TEXTS

### Start clause context:
{start_text}

### End clause context:
{end_text}

{intermediate_texts}

## ANNOTATOR'S GOLD-STANDARD EXAMPLES (study these patterns)

### Example 1 — Edge case that defeats binary logic
"A container ship has upper deck plate thickness 105 mm using NV-47 steel.
Does Pt5 Ch2 Sec10 apply to this plate, and what additional design action is needed?"
- Correct answer: NOT simply "applicable" or "not applicable" — for plates > 100mm,
  the application must be agreed with the Society on a case-by-case basis.
- Why this works: tests whether the model recognizes a non-binary edge case.
  Software/LLMs gravitate to yes/no — this catches that.

### Example 2 — Hidden gating condition that overrides downstream logic
"A container ship has hatch coaming thickness 45 mm with steel grade NV-47, and
upper deck thickness 70 mm with NV-40. Does the upper deck steel grade require the
suffix BCA1?"
- Correct answer: NO — because hatch coaming thickness (45mm) is below the Table 1
  threshold (50mm), countermeasures are not required REGARDLESS of upper deck
  thickness or grade. The crack arrest requirement is gated by the coaming.
- Why this works: tests whether the model notices a one-sentence gating condition
  that kills the entire downstream chain. Easy to miss; critical to compliance.

### Example 3 — Application perspective on tables
"For a Buckling assessment of shear stress in hogging condition at position x < L/2,
which value of Q_SW applies?"
- Correct answer: requires understanding what "Buckling assessment" maps to in the
  table's column structure (chain: Sec.4 [2.5] → [2.3] → [2.2] → Table 3 → Sec.3)
- Why this works: doesn't ask "what's in row 3 of Table 3?" — asks what assessment
  TYPE the column applies to. Tests COMPREHENSION not READING.

### Example 4 — Cross-formula comparison across volumes
"When calculating net thickness for Buckling assessment per Pt5 Ch2 Sec4 [2.3],
what corrosion addition is used and how does the resulting net thickness formula
differ from the general-purpose net thickness formula in Pt3 Ch2 Sec2?"
- Correct answer: this Pt5 formula is special-use for hull girder strength /
  buckling / ultimate strength. Pt3 formula is general-purpose. Different t_c.
- Why this works: forces the model to recognize that the SAME concept (net
  thickness) has TWO different formulas in TWO different volumes, used for
  different purposes.

## CORE PRINCIPLES (from domain expert annotator)

### 1. SHIP DESIGN, NOT RULE READING
- RIGHT: "A container ship has B=38m, plate t=55mm NV-47 at sheer strake within
  0.4L. What material class is required?"
- WRONG: "What does Pt5 Ch2 Sec10 [2.1.4] say about material grades?"
- WRONG: "Which section governs material grade selection?"
- The model should never see section numbers in the question. It must FIND them.

### 2. EDGE CASES THAT DEFEAT BINARY LOGIC
- Some questions should hit edge cases where the answer is NOT a simple pass/fail
- Example from annotator: "If t = 105mm NV47, does Sec10 apply?"
  → Answer is NOT yes/no. It's "case-by-case agreement with Society required"
  → Software gravitates to binary; this catches that
- Look for thresholds, ranges, exceptions in the start clause text and probe them

### 3. HIDDEN GATING CONDITIONS
- Some questions should test whether the model notices a SHORT plain-text sentence
  that overrides downstream logic
- Example from annotator: "If hatch coaming < 50mm, NO crack arrest steel required
  REGARDLESS of upper deck thickness or grade"
  → One sentence kills the entire downstream chain
- If you see such a gating sentence in the clause text, prefer questions that test it

### 4. APPLICATION PERSPECTIVE FOR TABLES/FORMULAS
- DO NOT ask "what's in row 3 of Table 1?"
- DO ask "for a Buckling assessment of shear stress in hogging at x < L/2, which
  Q_SW value applies?" — this requires understanding what the table COLUMN means
- DO NOT ask "what is the formula for f_tCG?"
- DO ask "for a centre girder with span ℓ=5m, what is f_tCG used for and what
  value does it take?" — tests comprehension of the formula's USE

### 5. CAUSAL DIRECTION
- Cause → effect, never reversed
- WRONG: "Given plate thickness 55mm, what should ship length be?"
- RIGHT: "Given ship length 320m and plate thickness 55mm, what class is required?"

### 6. NO CONTAINMENT REDUNDANCY
- If the question requires multiple sub-answers, don't ask the sub-answers separately
- Example: don't ask "what category is the sheer strake?" then "what class for
  Special category?" — combine into one question testing both

## DISTRACTOR DESIGN — MAKE THEM HARD

All 4 options must look plausible. Distractors come from:

| Type | Source |
|------|--------|
| Wrong table row | Adjacent row in the same lookup table (e.g., NV-36 vs NV-47) |
| Wrong column | Right row but wrong column (e.g., outside 0.4L vs within 0.4L) |
| Wrong branch | Value from a different conditional (e.g., sagging vs hogging) |
| Intermediate value | Stops one hop early in the chain |
| Formula error | Plausible arithmetic mistake (wrong sign, missing +1 term) |
| Close numeric | Adjacent value from same table or formula |

For TABLE-BASED questions: at least 2 distractors must come from REAL VALUES in
the SAME TABLE (different rows or columns). Read the table data from the clause
texts above and pick actual cell values for distractors.

For FORMULA questions: at least 1 distractor must be the result of a plausible
arithmetic error (wrong sign, missing term, swapped operands).

NEVER include an option that is obviously absurd. If you can spot the wrong answer
without thinking, the question is too easy.

## CONFIDENCE LABELING (CRITICAL)

You must label your confidence honestly. Default to dTx — only use "verified"
when the answer is a literal value directly stated in the clause text with no
interpretation needed.

- **verified**: 100% certain. The correct answer is a direct quote or single-cell
  table value with no calculation, no conditional branching, no judgment.
  Example: "What is the maximum hatch coaming clearance per [3.1.7]?" — answer
  is a literal "25 mm" stated in the text.

- **dT0–dT4**: You believe the answer is in the text but cannot be 100% sure.
  Pick the d-tier matching difficulty:
  - **dT0**: Simple lookup, low confidence (e.g., didn't fully parse a complex sentence)
  - **dT1**: Single-section formula or table, low confidence on parameters
  - **dT2**: Cross-section reasoning, low confidence on which branch applies
  - **dT3**: Multi-hop chain, low confidence on intermediate steps
  - **dT4**: Cross-volume reasoning with table/formula interpretation, low confidence

A wrong "verified" answer is much worse than a correct "dT2" answer. When the
question involves: table navigation with multiple columns, formula application
with multiple branches, or interpretation of a conditional clause — DEFAULT TO dTx.

## REJECTION CRITERIA (skip these chains entirely)

Return `null` (an empty object `{{"skip": true, "reason": "..."}}`) if:

1. The chain endpoint is a definitions/glossary section — no ship design question
   can be built from "what does symbol X mean"
2. The clause texts only contain amendment logs or change history, not regulatory
   requirements
3. The chain doesn't reach any table, formula, or conditional requirement —
   it's a chain of "see also" pointers with no real content at the end
4. You cannot construct a question that meets the SHIP DESIGN, NOT RULE READING
   principle — if the only meaningful question is "which clause governs X?",
   skip the chain

## CHAIN UNIQUENESS
This chain is one of many. Make sure your question tests something specific to
THIS chain — not a generic question that could apply to any chain. The unique
details of THIS chain's clauses (specific tables, specific formulas, specific
gating conditions) should be central to the question.

## OUTPUT: single JSON object, no fences.
{{
  "question": "A naval architect is verifying [concrete design with specific parameters]...",
  "options": {{"A":"...","B":"...","C":"...","D":"..."}},
  "correct": "A|B|C|D",
  "confidence": "verified|dT0|dT1|dT2|dT3|dT4",
  "derivation": "Step 1 (start clause): ... Step 2 (follow ref): ... Step 3 (end clause): ...",
  "distractors_rationale": "A=[source of this distractor]; B=[source]; C=[correct]; D=[source]",
  "evidence_location": "start: [clause] → end: [clause]",
  "question_type": "design_verification|edge_case|gating_condition|application_lookup",
  "content_operation": "lookup|table|formula|compliance",
  "design_parameters": {{"key":"value"}},
  "tests_edge_case": true_or_false,
  "tests_gating_condition": true_or_false
}}
"""


def generate_question_for_chain(
    chain: dict,
    sections_dir: Path,
    client: LLMClient,
    model: str,
) -> dict | None:
    """Generate one question for a mechanically-selected chain."""

    start = chain["start"]
    end = chain["end"]
    path = chain["path"]
    edges = chain["edges"]

    # Load clause texts
    start_text = load_section_text(sections_dir, start)
    end_text = load_section_text(sections_dir, end)

    if not start_text or not end_text:
        return None

    # Extract relevant clause context from edges
    start_clause = edges[0].get("clause", "") if edges else ""
    end_clause = edges[-1].get("clause", "") if edges else ""

    # Get clause-specific text
    start_excerpt = extract_clause_text(start_text, start_clause) if start_clause else start_text[:2000]
    end_excerpt = extract_clause_text(end_text, end_clause) if end_clause else end_text[:2000]

    # Intermediate texts for multi-hop chains
    intermediate_parts = []
    if len(path) > 2:
        for i, node in enumerate(path[1:-1]):
            mid_text = load_section_text(sections_dir, node)
            if mid_text:
                mid_clause = edges[i+1].get("clause", "") if i+1 < len(edges) else ""
                mid_excerpt = extract_clause_text(mid_text, mid_clause) if mid_clause else mid_text[:1500]
                intermediate_parts.append(f"### Intermediate clause ({node}):\n{mid_excerpt[:1500]}")

    intermediate_block = "\n\n".join(intermediate_parts) if intermediate_parts else "(direct chain, no intermediates)"

    chain_path = " → ".join(f"{e.get('raw', '?')}" for e in edges)

    prompt = PARAMETER_PROMPT.format(
        start_clause=f"{start} [{start_clause}]",
        chain_path=chain_path,
        end_clause=f"{end} [{end_clause}]",
        start_text=start_excerpt[:3000],
        end_text=end_excerpt[:3000],
        intermediate_texts=intermediate_block[:3000],
    )

    cache_key = f"chain_v5b_{start}_{end}_{chain['depth']}".replace(".", "_")
    cache_key = re.sub(r'[^a-zA-Z0-9_]', '_', cache_key)

    response = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=4096,
        temperature=0.3,
        cache_key=cache_key,
    )

    # Parse
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

    # Honor explicit skip
    if q.get("skip") is True:
        return {"_skipped": True, "reason": q.get("reason", "model requested skip")}

    # Validate required fields
    if not q.get("question") or not q.get("options") or not q.get("correct"):
        return None

    # Tag with chain metadata
    classification = classify_chain(chain)
    q["chain"] = {
        "start": start,
        "end": end,
        "path": path,
        "depth": chain["depth"],
        "edges_raw": [e.get("raw", "") for e in edges],
    }
    q["span"] = classification["span"]
    q["hop_count"] = classification["hop_count"]
    q["tier"] = classification["legacy_tier"]
    q["source_section"] = start
    q["cross_ref_target"] = end
    q["generation_method"] = "mechanical_chain_v5"
    q.setdefault("confidence", "dT" + str(classification["legacy_tier"]))
    q.setdefault("tests_edge_case", False)
    q.setdefault("tests_gating_condition", False)
    q.setdefault("design_parameters", {})
    q["review"] = {"question_clear": None, "answer_correct": None, "tier_correct": None,
                   "expert_resolved_dtx": None}

    return q


# Sections that are definition/glossary/amendment-only — exclude as chain endpoints
# because they don't support ship design verification questions
DEFINITION_SECTIONS = {
    "Pt3.Ch1.Sec4",   # General definitions, symbols, abbreviations
    "Pt3.Ch1.Sec1",   # Introduction
    "Pt3.Ch1.Sec2",   # Application
    "Pt3.Ch1.Sec3",   # Verification of compliance
    "Pt1.Ch1.Sec1",   # General
    "Pt1.Ch1.Sec2",   # Definitions
}

# Sections that contain only amendment logs / change history
# (none currently identified mechanically — add manually if encountered)
AMENDMENT_SECTIONS = set()


def is_useful_chain(chain: dict) -> bool:
    """Filter out chains that can't produce ship design verification questions."""
    end = chain["end"]

    # Reject chains ending at definition sections — they only enable lookup questions
    if end in DEFINITION_SECTIONS:
        return False

    # Reject chains where any intermediate node is a definition section
    # (the chain is really just "go look up a definition")
    for node in chain.get("path", [])[1:]:
        if node in DEFINITION_SECTIONS:
            return False

    # Reject single-edge chains where the target_clause is just a section root
    # (no specific clause means no specific table/formula to test)
    edges = chain.get("edges", [])
    if edges and len(edges) == 1:
        clause = edges[0].get("clause", "")
        # Reject "[3]" with no sub-numbering — too generic
        if re.fullmatch(r'\d+', clause):
            return False

    return True


def select_chains(all_chains: list[dict], max_per_cell: int = 15) -> list[dict]:
    """Select diverse chains across the taxonomy grid.
    Enforce uniqueness on the FULL EDGE SEQUENCE (clauses), not just (start, end).
    Two chains with the same start/end but different clause refs are distinct."""

    # Filter out non-useful chains first
    before = len(all_chains)
    all_chains = [c for c in all_chains if is_useful_chain(c)]
    log(f"  Filtered {before - len(all_chains)} chains (definition/amendment endpoints)")

    # Group by (span, hop_count)
    cells = defaultdict(list)
    for c in all_chains:
        cls = classify_chain(c)
        cell = (cls["span"], cls["hop_count"])
        cells[cell].append(c)

    selected = []
    global_clause_keys = set()  # Cross-cell deduplication on clause sequences

    for cell_key, cell_chains in sorted(cells.items()):
        # Build a fingerprint based on the clause sequence (target_clause for each edge)
        cell_unique = []
        cell_seen = set()
        for c in cell_chains:
            edges = c.get("edges", [])
            clause_seq = tuple(e.get("target_clause", e.get("clause", "")) for e in edges)
            fingerprint = (c["start"], c["end"], clause_seq)
            if fingerprint in cell_seen or fingerprint in global_clause_keys:
                continue
            cell_seen.add(fingerprint)
            global_clause_keys.add(fingerprint)
            cell_unique.append(c)

        # Sample up to max_per_cell
        sampled = cell_unique[:max_per_cell]
        selected.extend(sampled)
        log(f"  Cell {cell_key}: {len(cell_chains)} chains → {len(cell_unique)} unique → {len(sampled)} selected")

    return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph", default="/workspace/xref_graph.json")
    parser.add_argument("--sections-dir", default="/workspace/ocx_explanation")
    parser.add_argument("--out", default="/workspace/chain_questions_v4.json")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--max-per-cell", type=int, default=15)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    graph = load_xref_graph(args.graph)
    chains = graph["chains"]
    log(f"Loaded {len(chains)} chains from graph")

    log("Selecting diverse chains...")
    selected = select_chains(chains, args.max_per_cell)
    log(f"Selected {len(selected)} chains")

    if args.dry_run:
        selected = selected[:4]
        log(f"Dry run: {len(selected)} chains")

    client = LLMClient(
        cache_dir="/workspace/homerun/chain_qgen_cache",
        token_log_path="/workspace/homerun/token_log_chain.jsonl",
    )

    sections_dir = Path(args.sections_dir)
    all_questions = []
    skipped_chains = []

    for i, chain in enumerate(selected):
        cls = classify_chain(chain)
        path_str = " → ".join(chain["path"])
        log(f"  [{i+1}/{len(selected)}] {path_str} (span={cls['span']}, hops={cls['hop_count']})")

        q = generate_question_for_chain(chain, sections_dir, client, args.model)
        if q is None:
            log(f"    ✗ failed (parse/missing fields)")
        elif q.get("_skipped"):
            log(f"    ↷ skipped: {q.get('reason','')[:80]}")
            skipped_chains.append({"chain": chain, "reason": q.get("reason", "")})
        else:
            all_questions.append(q)
            conf = q.get("confidence", "?")
            edge_case = "EDGE" if q.get("tests_edge_case") else ""
            gating = "GATE" if q.get("tests_gating_condition") else ""
            tags = " ".join(t for t in [conf, edge_case, gating] if t)
            log(f"    ✓ {q.get('content_operation', '?')} [{tags}]")

    # Assign IDs
    for i, q in enumerate(all_questions):
        q["id"] = f"CHAIN_{i+1:04d}"

    # Save
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_questions, indent=2, ensure_ascii=False))

    # Summary
    from collections import Counter
    spans = Counter(q["span"] for q in all_questions)
    hops = Counter(q["hop_count"] for q in all_questions)
    ops = Counter(q.get("content_operation", "?") for q in all_questions)
    confs = Counter(q.get("confidence", "?") for q in all_questions)
    edges = sum(1 for q in all_questions if q.get("tests_edge_case"))
    gates = sum(1 for q in all_questions if q.get("tests_gating_condition"))

    log(f"\nSaved {len(all_questions)} questions to {out_path}")
    log(f"  Skipped: {len(skipped_chains)}")
    log(f"  Spans: {dict(spans)}")
    log(f"  Hops: {dict(hops)}")
    log(f"  Operations: {dict(ops)}")
    log(f"  Confidence: {dict(confs)}")
    log(f"  Edge case questions: {edges}")
    log(f"  Gating condition questions: {gates}")
    log(f"  Token usage: {client.get_token_usage()}")


if __name__ == "__main__":
    main()
