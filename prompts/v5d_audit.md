# Appendix C: The v5d audit prompt (verbatim)

This appendix reproduces the locked v5d audit prompt referenced throughout §3.3 and §4.1. The prompt was developed against the 148-question DNV TRAIN audit and applied unchanged to DNV TEST, the 300-item DNV remaining pool, the 288-item Basel III §217 pool, and the cross-vendor Sonnet 4.6 replay. No section of this prompt was modified between TRAIN derivation and downstream application.

The prompt is composed of three blocks: a base v5 audit definition, a strong conservative-bias replacement, and a pattern-guardrail appendix targeting the three failure modes (E1/E2/E3 in the paper, T1/T2/T3 in the prompt) observed during TRAIN audit. The assembled v5d text below is what the runner sends to the auditor model verbatim, prepended to the per-item input block (`item_id`, `tier`, `source_section`, `question_text`, `required_facts`, `annotator_grounding`, `source_text`).

The auditor model is GPT-5.4 with `model_reasoning_effort=xhigh`, `--sandbox read-only`, structured-output schema enforcement (`schema_v5d.json`), and per-call timeout 600s. Source code: `/workspace/regbench_pilot/filter_dev/filter_prompts.py` (variable `FILTER_PROMPT_V5_CONSERVATIVE_PATTERN`, registered as `PROMPTS["v5d"]`).

## Block 1 — Base v5 audit (`FILTER_PROMPT_V5_BASE`)

```
You audit a regulatory reasoning benchmark. For each required_fact, decide whether
the claim is supported by the verbatim source text or contradicted by it. Err
toward `correct` unless the source contains a concrete, verifiable contradiction.

Goal: catch real, substantive defects in the gold-standard facts. Do NOT flag on
paraphrase nuance, softer/stronger wording, or details the source is silent on.

## Input
- `question_text` — scenario the benchmark poses
- `required_facts` — atomic facts the benchmark's gold answer must satisfy
- `annotator_grounding` — author's explanation of the reasoning chain
  (verify against source; not authoritative)
- `source_text` — verbatim regulatory excerpts. May contain LaTeX/math, table
  artifacts from PDF extraction, or OCR noise. Parse through these.

## Clause-reference convention (generic across regulatory domains)
- Section identifiers (e.g. `Pt.X.Ch.Y.Sec.Z`, `§N.M`, `Title N, Part M, Section K`)
  identify a top-level unit.
- Bracketed sub-identifiers like `[1.1.1]`, `[2.4.2]`, `[9.5.8]` are sub-clauses
  NESTED within the cited section's text. Find them by heading within the
  section's file, not as separate sections.
- Tables often carry numbered identifiers (e.g. `Table 11`) and contain rows
  and columns keyed on scenario parameters. When a fact cites a specific table
  row/column, verify the exact cell.

## Verdicts (per required_fact)
- **correct** — the fact is supported by source text. Acceptable if:
    (a) a specific passage/cell/formula in source directly supports the fact, OR
    (b) the fact is a reasonable paraphrase of clearly-matching source text, OR
    (c) the source is silent on the specific detail and the fact is a neutral
        restatement of the question setup.
  Cite a supporting span in `reason` whenever possible.
- **incorrect** — reserved for SUBSTANTIVE, VERIFIABLE CONTRADICTIONS only. Flag
  `incorrect` ONLY when ALL of:
    (a) the fact asserts a concrete, binding element: a specific numeric value,
        table cell, coefficient, formula operator, row/column selection, verdict,
        or binary compliance decision,
    (b) the source clearly contains the matching concrete element (specific
        clause, table row, formula),
    (c) the source's element says something materially different from what the
        fact claims.
  Do NOT mark `incorrect` on: paraphrase nuance, softer/stronger wording ("may"
  vs "shall"), scope implicit in context, missing filler text, interpretations
  a careful reader could agree with, or implications the fact draws from source
  content.
- **uncertain** — source is genuinely ambiguous, OR the cited clause cannot be
  located despite careful search, OR the fact's concrete element cannot be
  verified either way.
- **hallucinated_citation** — the fact cites a specific clause number, table,
  or formula identifier that does NOT appear in source_text. Do NOT use for
  sub-clauses that exist in the source; use only for genuinely missing
  identifiers.

## Item-level flag rule (TIGHTENED — substantive only)
- `pass` — every required_fact is `correct`, OR non-correct verdicts are limited
  to `uncertain`, OR the only non-correct verdicts are on paraphrase nuance
  rather than substantive elements.
- `flag` — at least one fact is `incorrect` with a SUBSTANTIVE, VERIFIABLE
  CONTRADICTION (numeric/binding/verdict-changing), OR at least one
  `hallucinated_citation`.

If the only flags are `uncertain` or paraphrase-level disagreements, do NOT flag.
```

## Block 2 — Conservative-bias replacement (Knob 1, `FILTER_PROMPT_V5_CONSERVATIVE`)

In the base block, the single sentence beginning *"Default bias: benchmark items are generally well-authored..."* is replaced verbatim by the following two paragraphs:

```
Default bias (strong): benchmark items are generally well-authored. When you
are uncertain whether a fact is wrong, choose `correct`. It is acceptable to
miss some subtle defects in exchange for avoiding over-flagging. Only commit
to `incorrect` when the concrete evidence in source contradicts the fact's
concrete claim with near-certainty.

A skeptical second reader should be able to inspect your `reason` and
immediately confirm the contradiction against the quoted source span without
needing to interpret additional context.
```

## Block 3 — JSON output schema

Appears at the end of the base block, unchanged across all three v5 variants:

```
## Output (JSON only)
{
  "per_fact": [
    {"fact_index": 0,
     "verdict": "correct|incorrect|uncertain|hallucinated_citation",
     "reason": "<short justification; quote supporting or contradicting passage>"}
  ],
  "item_verdict": "pass|flag",
  "overall_confidence": "high|medium|low",
  "summary": "<one-sentence rationale>"
}

No prose before or after the JSON.
```

The structured-output schema (`schema_v5d.json`) is enforced by the runner via the codex CLI `--output-schema` flag.

## Block 4 — Pattern guardrails appendix (`FILTER_PROMPT_V5_PATTERN_GUARDS_APPENDIX`)

Appended verbatim after Block 1 (with Block 2's replacement applied) to form the final v5d prompt.

```
## Specific defect-pattern guardrails
   (flag ONLY if ALL pattern triggers fire precisely)

**Pattern T1 — Table row/column mismatch.** A required_fact cites a specific
table cell value. To flag `incorrect` on this pattern, ALL must hold:
  - The cited table is present in source_text.
  - The specific row (scenario-matching) and column are identifiable from the
    source.
  - The value in that exact cell in source materially differs from what the
    fact claims.
If any step cannot be verified, use `uncertain` or `correct`, not `incorrect`.

**Pattern T2 — Formula integrity.** A required_fact states a formula or derived
numeric result. To flag `incorrect` on this pattern, ALL must hold:
  - The source contains a matching formula for the same quantity the fact
    derives.
  - The fact's formula differs in a concrete structural way: wrong operator,
    missing or extra variable, wrong exponent or coefficient, wrong sign.
  - OR the fact's derived numeric result is computed from a substitution
    inconsistent with source.
Do NOT flag on notation style, equivalent algebraic rearrangements, rounding,
or paraphrase of symbol meanings.

**Pattern T3 — Default vs special branch.** A required_fact asserts a
conclusion that depends on which of a default/special regulatory branch
applies. To flag `incorrect`, ALL must hold:
  - The regulation explicitly has default and special branches with materially
    different answers.
  - The scenario describes parameters that unambiguously select one branch.
  - The fact's answer uses the OTHER branch.
Do NOT flag if the scenario leaves the branch choice ambiguous — that is a
question-authoring concern, not a fact defect; in such a case, use `correct`
and note the ambiguity in `reason`.
```

## Knob naming and ablation registry

The paper's E1/E2/E3 defect taxonomy corresponds one-to-one with the prompt's T1/T2/T3 pattern guardrails:

| Paper name | Prompt name | Defect category |
|---|---|---|
| E1 chart/table row-column mismatch | T1 Table row/column mismatch | Cell-level table indexing errors |
| E2 formula integrity | T2 Formula integrity | Operator / variable / exponent / sign substitution |
| E3 default-vs-special-case ambiguity | T3 Default vs special branch | Wrong regulatory branch applied |

The four ablation variants registered in `filter_prompts.py` (`PROMPTS` dict):

| Tag | Composition | Description |
|---|---|---|
| `v5a` | Base only (Block 1 + Block 3) | Tightened `incorrect` rule; no extra bias, no pattern guards |
| `v5b` | + conservative bias (Block 2) | Adds the strong default-bias replacement |
| `v5c` | + pattern guards (Block 4) | Adds T1/T2/T3 guardrails on top of `v5a` |
| **`v5d`** | **Both Block 2 and Block 4** | Used in all paper experiments |

## Per-item input format (constructed by the runner)

After the v5d prompt, the runner appends an item-specific block produced by `build_filter_input()`:

```
## Item to audit

**item_id:** {id}
**tier:** {tier}
**source_section (anchor):** {anchor}

### question_text
{question_text}

### required_facts
0. {fact_0}
1. {fact_1}
...

### annotator_grounding
- **start_clause:** ...
- **end_clause:** ...
- **chain_summary:** ...
- **expected_derivation:** ...

### source_text (verbatim)
{ALL referenced section files concatenated, anchor first;
loaded by source_loader.load_source_texts (DNV) or
the §217.X-resolving loader (Basel)}

## Now output the JSON verdict per the schema.
```

The source-text gathering uses **broad loading**: every Pt.Ch.Sec (DNV) or §217.X (Basel) reference appearing in any of the item's text fields (anchor, grounding fields, required_facts, question_text) is resolved against the corpus and concatenated, with caps of 140K bytes per section and 500K bytes total to fit context.

## Appendix C.2 — Structured-output JSON Schema (`schema_v5d.json`)

The codex CLI's `--output-schema` flag enforces this JSON-Schema (draft 2020-12 compatible) on every response. Validation failures are surfaced as `error: json_decode` in the runner output and re-tried in resume mode. Any verdict the auditor produces that is not in the enumerated lists below is rejected at the API boundary, before the response reaches the runner.

```json
{
  "type": "object",
  "required": [
    "per_fact",
    "item_verdict",
    "suggested_fix",
    "overall_confidence",
    "summary"
  ],
  "properties": {
    "per_fact": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["fact_index", "verdict", "reason", "suggested_correction"],
        "properties": {
          "fact_index": {"type": "integer"},
          "verdict": {
            "type": "string",
            "enum": [
              "correct",
              "incorrect",
              "uncertain",
              "hallucinated_citation",
              "E1_chart_mismatch",
              "E2_formula_broken",
              "E3_default_ambiguous"
            ]
          },
          "reason": {"type": "string"},
          "suggested_correction": {"type": "string"}
        },
        "additionalProperties": false
      }
    },
    "item_verdict": {
      "type": "string",
      "enum": ["pass", "flag"]
    },
    "suggested_fix": {
      "type": "string",
      "enum": [
        "no_fix_needed",
        "fix_answer",
        "fix_question",
        "kill_question"
      ]
    },
    "overall_confidence": {
      "type": "string",
      "enum": ["high", "medium", "low"]
    },
    "summary": {"type": "string"}
  },
  "additionalProperties": false
}
```

**Schema notes:**

1. The per-fact `verdict` enum carries seven values. The four core verdicts (`correct`, `incorrect`, `uncertain`, `hallucinated_citation`) are emitted by the v5d prompt's body. The three E-prefixed verdicts (`E1_chart_mismatch`, `E2_formula_broken`, `E3_default_ambiguous`) were a legacy v4 codepath kept in the schema for forward-compatibility; **v5d does not produce E-prefixed verdicts in practice** — its pattern-guardrail block (T1/T2/T3) does NOT instruct the model to emit them, and inspection of all v5d outputs across DNV TRAIN/TEST, DNV remaining-300, and Basel confirms zero E-prefixed verdicts under v5d. They remain in the enum to keep the schema valid against legacy v4-tagged outputs already in the audit traces.

2. `suggested_fix` is enforced even though the v5d prompt body does not explicitly request it. The model populates it from a small follow-on instruction the runner concatenates after the main prompt; in practice it is one of `no_fix_needed`, `fix_answer` (the dominant repair-eligible disposition), `fix_question` (rare), or `kill_question` (rare).

3. `overall_confidence` is self-reported metadata. The paper does not stratify analysis on this field; full-text agreement and the rule-of-three upper bound on miss rate (§3.3) are computed without conditioning on the auditor's confidence self-report.

4. `additionalProperties: false` at both the root and `per_fact` levels enforces strict closed-world output, preventing the auditor from emitting freestyle commentary outside the schema.

## Reproducibility

The prompt is invoked via the codex CLI:

```bash
codex exec \
  --model gpt-5.4 \
  -c model_reasoning_effort=xhigh \
  --sandbox read-only \
  --skip-git-repo-check \
  --output-schema /path/to/schema_v5d.json \
  --output-last-message /path/to/raw/<item_id>.txt \
  --color never
```

with the assembled prompt + item block fed via stdin. The same invocation is used by `filter_runner.py` (DNV TRAIN/TEST), `run_v5d_dnv_remaining300.py` (DNV remaining 300), and `run_v5d_basel_full.py` (Basel) — only the source-gathering function and the input pool differ.
