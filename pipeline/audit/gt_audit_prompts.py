"""Prompt templates for the three-module auto GT audit pipeline."""

# -------------------- Module 1: Source-integrity audit --------------------

SOURCE_INTEGRITY_PROMPT = """\
You are auditing the fidelity of a PDF-to-markdown conversion of regulatory text. Your job is to identify signs that the markdown was damaged during extraction.

## SOURCE SECTION (polished markdown, as downstream pipelines consume it)
Section ID: {section_id}
Head: {section_head}

```markdown
{section_text}
```

## YOUR TASK

Look for conversion-damage signals. Specifically check:

1. **Tables**: for each visible table, count the column headers and check that every data row has a matching number of values. Misaligned rows, missing values, or cells with zero-width markers (e.g. "—" only in some cells where numbers are expected) are damage indicators.
2. **Formulas**: check for balanced parentheses/brackets/braces. Detect broken exponents (e.g. `^{{2}}` rendered as `2` or `^2` without clear association). Detect fragmented equations where what should be one line spans multiple.
3. **Symbol/unit preservation**: for symbols like φ, θ, σ, units like kN/m², detect if they were mangled (e.g. "kN/m2" when source should have superscript).
4. **Row-cell content collapse**: detect cells containing many rows' worth of text jammed together (common MinerU failure mode on complex tables).

Output a single JSON object (no fences):

{{
  "integrity_status": "clean" | "minor_damage" | "major_damage",
  "damage_signals": [
    {{"type": "table_column_mismatch"|"broken_formula"|"symbol_mangling"|"cell_collapse"|"other",
      "location_hint": "e.g. Table 11 row 'CyR'",
      "detail": "specific description of what looks wrong"}}
  ],
  "suspect_content_areas": ["any row/table/formula identifiers a content-audit agent should treat with reduced confidence"],
  "overall_confidence_weight": 0.0-1.0
}}

The overall_confidence_weight is how much downstream audit agents should trust this section's content: 1.0 if extraction looks pristine, lower if damage makes semantic judgments less reliable. Output only the JSON.
"""


# -------------------- Module 2: Sonnet canonical pairing --------------------

SONNET_PAIRING_PROMPT = """\
You are a domain-expert auditor reading regulatory text to establish the canonical answer and required facts for a question.

## QUESTION
{question_text}

## REQUIRED FACTS (as generated; under audit — your job is to verify, not accept)
{required_facts_numbered}

## CITED REGULATORY SECTIONS (ground truth source)
{section_bundle}

## SOURCE-INTEGRITY NOTICE
{integrity_notice}

## YOUR TASK

Step 1 — Read the question carefully. Identify every scenario-specific factor: class notation (e.g. bulk carrier, container ship), size thresholds, conditions (e.g. fatigue assessment performed, AC-III vs AC-II), structural element type, or any parameter that could trigger a regulatory override of general defaults.

Step 2 — Establish the CANONICAL answer the regulation requires, given the scenario and only the cited sections. Work it out yourself; do not echo the given required_facts.

Step 3 — Enumerate the atomic facts your canonical answer depends on. These are what a correct required_facts list for this question should contain.

Step 4 — Compare the given required_facts (above) to your canonical fact set. For each given fact, verdict it as:
- "verified": fact is consistent with your canonical reading AND supported by the cited sections
- "wrong_value": fact asserts a specific value/formula that contradicts the source
- "missing_override": fact applies a general default but the scenario triggers a class-notation or conditional override that the fact ignores
- "conflated_paragraph": fact borrows wording from a related paragraph whose applicability differs
- "uncertain": source is ambiguous; cannot confirm or contradict the fact
- "conditional_flatten": fact states an unconditional claim where the source gives conditional branches

**Flag discipline (L2 — quote-grounded verdicts).** Any verdict other than `verified` or `uncertain` must be backed by a verbatim `contradicting_source_quote` drawn from the cited sections that directly contradicts the fact. If you cannot produce such a quote, the correct verdict is `uncertain`, NOT `wrong_value` / `missing_override` / `conflated_paragraph` / `conditional_flatten`. Fabricated quotes or paraphrases are disqualifying; quote the source exactly. The counter-quote should be short (≤2 sentences) and targeted at the specific claim in the fact.

**Default-tolerance rule (L4 — calibrated conditional handling).** When a source rule gives conditional branches (e.g., "X for case A, Y for case B"), do NOT use verdict `conditional_flatten` unless the scenario explicitly selects a non-default branch. A fact that applies one valid branch without stating the triggering condition is tolerable when: (a) the scenario does not name a classification / mode / condition that unambiguously triggers a specific branch, (b) the chosen branch is one of the valid options under the source, (c) no override paragraph requires a different branch for the scenario's class. Silence about which branch applies is not an error; only flag `conditional_flatten` when the scenario actively selects a different branch from the one the fact applies. This does NOT relax `missing_override`: a classification explicitly named in the scenario (e.g., "bulk carrier", "container ship") still triggers the override check.

Output JSON:

{{
  "canonical_answer_summary": "one paragraph, your own reading of what the scenario demands",
  "override_triggers_detected": ["bulk carrier → Pt.5 override", "..."],
  "canonical_fact_set": ["fact 1", "fact 2", ...],
  "per_fact_verdicts": [
    {{"idx": 1,
      "verdict": "verified|wrong_value|missing_override|conflated_paragraph|uncertain|conditional_flatten",
      "contradicting_source_quote": "verbatim quote from cited sections — REQUIRED unless verdict is verified or uncertain; empty string otherwise",
      "rationale": "one sentence linking the quote to why the fact is wrong",
      "proposed_correction": "..." or null}},
    ...
  ]
}}
"""


# -------------------- Module 3: GPT-5.4 adversarial match-up --------------------

ADVERSARIAL_VALIDATION_PROMPT = """\
You are an independent second-opinion auditor. Another auditor has already read the same question and produced a canonical analysis. Your job is to agree or challenge their reading per-fact.

Do not defer to the first auditor. If their canonical reading has errors, call them out.

## QUESTION
{question_text}

## REQUIRED FACTS (under audit)
{required_facts_numbered}

## CITED REGULATORY SECTIONS
{section_bundle}

## SOURCE-INTEGRITY NOTICE
{integrity_notice}

## FIRST AUDITOR'S CANONICAL READING AND VERDICTS

Canonical answer summary: {canonical_answer_summary}

Override triggers they flagged: {override_triggers}

Their per-fact verdicts:
{first_auditor_verdicts}

## YOUR TASK

For each required fact, independently verdict it against the cited sections. Then compare your verdict to the first auditor's:

- "confirmed": you agree with first auditor
- "dispute": you disagree. Specify whether you think the first auditor was too lenient (missed an error), too harsh (flagged a correct fact), or flagged the right issue with the wrong reason

**Flag discipline (L2 — quote-grounded verdicts).** Any `my_verdict` other than `verified` or `uncertain` must be backed by a verbatim `contradicting_source_quote` drawn from the cited sections that directly contradicts the fact. If you cannot produce such a quote, the correct verdict is `uncertain`, NOT `wrong_value` / `missing_override` / `conflated_paragraph` / `conditional_flatten`. Fabricated quotes or paraphrases are disqualifying; quote the source exactly. Keep quotes short (≤2 sentences) and targeted at the specific claim in the fact. Do not inherit the first auditor's verdict without your own quote — if the first auditor flagged a fact but you cannot produce a counter-quote, dispute as `dispute_harsh` with verdict `uncertain` or `verified`.

**Default-tolerance rule (L4 — calibrated conditional handling).** When a source rule gives conditional branches (e.g., "X for case A, Y for case B"), do NOT use verdict `conditional_flatten` unless the scenario explicitly selects a non-default branch. A fact that applies one valid branch without stating the triggering condition is tolerable when: (a) the scenario does not name a classification / mode / condition that unambiguously triggers a specific branch, (b) the chosen branch is one of the valid options under the source, (c) no override paragraph requires a different branch for the scenario's class. Silence about which branch applies is not an error; only flag `conditional_flatten` when the scenario actively selects a different branch from the one the fact applies. This does NOT relax `missing_override`: a classification explicitly named in the scenario (e.g., "bulk carrier", "container ship") still triggers the override check. When the first auditor flagged `conditional_flatten` and the scenario is silent about the triggering condition, default to `dispute_harsh` with verdict `verified`.

Output JSON:

{{
  "per_fact_review": [
    {{"idx": 1,
      "my_verdict": "verified|wrong_value|missing_override|conflated_paragraph|uncertain|conditional_flatten",
      "contradicting_source_quote": "verbatim quote from cited sections — REQUIRED unless my_verdict is verified or uncertain; empty string otherwise",
      "agreement_with_first": "confirmed|dispute_lenient|dispute_harsh|dispute_reason",
      "rationale": "one sentence linking the quote to why the fact is wrong",
      "proposed_correction": "..." or null}}
  ],
  "agreement_rate": "N/M facts where I agree with first auditor",
  "source_integrity_concerns": ["any additional extraction damage I noticed that Module 1 missed"]
}}
"""
