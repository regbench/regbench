# Item schema

Each line of `data/{pilot,dnv,basel}.jsonl` is a single JSON object representing one benchmark item. The fields below are the public-release schema (internal generation/audit artifacts are stripped).

| Field | Type | Description |
|---|---|---|
| `id` | string | Stable item id. Prefixes: `R500_`/`F500_` (DNV main), `P50_` (DNV pilot), `B_T*_` (Basel) |
| `domain` | string | `dnv_ru_ship` \| `dnv_ru_ship_pilot` \| `basel_12cfr217` |
| `tier` | int | Chain depth, 0–4 (DNV) / 0–3 (Basel) |
| `source_section` | string | Anchor section identifier where reasoning starts |
| `chain` | object \| null | `{start, end, path, depth, ...}`. Basel items always carry an explicit chain object; for DNV the chain metadata lives inside `annotator_grounding` |
| `question_text` | string | Scenario-style question, self-contained. Chain identifiers are *not* leaked into the prompt |
| `format` | string | `mcq` \| `explanation` |
| `options` | list \| null | MCQ options (when `format == "mcq"`) |
| `correct` | string \| null | MCQ correct option key (when `format == "mcq"`) |
| `required_facts` | list[string] | Atomic propositions the answer must contain. **Strict-conjunction graded** |
| `annotator_grounding` | object | Source-grounded rationale (chain identifiers, target section quotes, derivation steps) |
| `scenario_parameters` | object | Numeric / categorical inputs that define the scenario |
| `tested_pattern` | string | Reasoning-pattern category (e.g. `survey_check`, `applicability_filter`, `quantitative_apply`) |
| `leak_check` | object | Leak-filter trace: regex pass + leaked tokens, if any |
| `gold_pages` | object | **(`pilot` only)** Human-verified evidence pages per chain step |

## Worked example (DNV `F500_0015`, Pt3.Ch12.Sec10, T2)

A 140 m general cargo vessel: freeing-port and hatch-cover compliance check.

- **Anchor:** `Pt3.Ch12.Sec10`
- **Chain:** `Pt3.Ch12.Sec10 → Sec1 [3.3.1] → Sec3 [4.1.2] → Sec2 [1.3]` (depth 3, T2: cross-section within the same chapter Pt3.Ch12)
- **Required facts (gold derivation):**
  1. Base freeing-port area for ℓ_b = 25 m is A = 0.07 × 25 = 1.75 m².
  2. Bulwark height of 1.5 m exceeds 1.2 m by 0.3 m, requiring an increase of 0.004 × 25 × 3 = 0.30 m², giving an adjusted base area of 2.05 m².
  3. No-sheer condition requires a 50 % increase, giving a minimum required area of 3.075 m² per side.
  4. The proposed 2.80 m² per side is insufficient and does not comply.
  5. The net bolt-area requirements per bolt for ordinary packed hatch covers are found in the access-openings section of the same chapter (Sec2 [1.3]).
  6. The cross-reference to bolt-area scantlings is reached via the internal-hatch securing requirements, which explicitly direct to the Sec2 [1.3] provisions.
- **Verdict:** the proposed 2.80 m²/side is insufficient; minimum required area is **3.075 m² per side**. Net bolt-area requirements: Pt3.Ch12.Sec2 [1.3].
- **Strict scoring:** an answer that lands the 3.075 m² verdict but omits e.g. fact 2 (bulwark-height adjustment) is graded **incorrect**.

See Appendix A of the paper for the full rendering and the JSON-tuple mapping to the formal task tuple from §3.1.
