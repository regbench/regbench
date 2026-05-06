# `pipeline/ingestion/` — DNV PDF → markdown

The DNV Ship Rules are not redistributable: upstream rights-holders retain copyright and the rulebook PDFs are gated behind a free DNV registration. RegBench therefore ships a **reproducible source-extraction pipeline** rather than the source text itself. To regenerate the markdown corpus that the rest of the pipeline operates on, you supply your own DNV PDFs and run the MinerU-based converter under `pdf_converter/`.

> Basel III §217 needs none of this — the public markdown source is consumed directly. Only DNV ingestion uses this folder.

## 1. Get the DNV PDFs

1. Register a free account at <https://www.dnv.com> and accept the terms-of-use that gate <https://rules.dnv.com>.
2. Download the rulebooks RegBench reasons over (Pt1 / Pt3 / Pt5 in the released benchmark; the same procedure works for Pt2 / Pt4 / Pt6 if you want to extend coverage):
   - **DNV-RU-SHIP-Pt1** General Regulations
   - **DNV-RU-SHIP-Pt3** Hull
   - **DNV-RU-SHIP-Pt5** Ship types
3. Use the section-level PDF export (one PDF per `Pt.X.Ch.Y.Sec.Z`) — that is the granularity the converter and the section-id regex expect.
4. The corpus snapshot used in the paper is **DNV Ship Rules edition 2024-07** (or later editions if you want to track current rules — the construction pipeline and v5d audit are corpus-independent, but item ids are tied to a specific snapshot).

## 2. Place them in the input tree

Drop the section PDFs into `pdf_converter/metadata_files/` using DNV's filename convention:

```
pdf_converter/metadata_files/
├── DNV-RU-SHIP-Pt1/
│   ├── DNV-RU-Pt1-Chap1-sec1.pdf
│   ├── DNV-RU-Pt1-Chap1-sec2.pdf
│   └── ...
├── DNV-RU-SHIP-Pt3/
│   └── ...
└── DNV-RU-SHIP-Pt5/
    └── ...
```

**The shipped `metadata_files/` and `metadata_markdown_files/` directories are intentionally empty.** Only `.gitkeep` markers are tracked. Do not commit DNV source PDFs back into the repo.

## 3. Run the converter

```bash
cd pipeline/ingestion/pdf_converter

# Convert every PDF under metadata_files/<part>/ to section-level markdown
# under metadata_markdown_files/<part>/<section-id>/.
bash pdf_to_md_Latex.sh metadata_files/DNV-RU-SHIP-Pt3 metadata_markdown_files/DNV-RU-SHIP-Pt3
```

`pdf_to_md_Latex.sh` is a thin loop over `pdf_to_latex.py`, which calls MinerU's pipeline backend (`Magic-PDF/mineru/`) with a few RegBench-specific post-processing passes (table continuation-row repair, footer masking, formula sanity checks).

Useful flags when calling `pdf_to_latex.py` directly:

| Flag | Meaning |
|---|---|
| `--input_file` | A single PDF to process (otherwise the script walks `--input_root`) |
| `--input_root` | Root directory the runner searches for `*.pdf` |
| `--output_dir` | Where section-level markdown is written |
| `--table_parser` | `text` (default; matches the paper) or `model` |
| `--footer_mask_ratio` | Fraction of page height masked as footer; `0.1` matches the paper |

## 4. Verify the markdown lines up with the section-id regex

`pipeline/graph/build_xref_graph.py` expects section identifiers shaped `Pt<N>.Ch<N>.Sec<N>`. The converter emits one folder per source section under `metadata_markdown_files/<part>/<section-id>/`, where `<section-id>` is the original PDF stem (e.g. `DNV-RU-Pt3-Chap12-sec10`). The graph builder normalises stem → identifier; if you change the input filename convention, also update `SECTION_ID_REGEX` at the top of `build_xref_graph.py`.

A quick smoke check after running the converter:

```bash
ls metadata_markdown_files/DNV-RU-SHIP-Pt3 | head
# Expect one directory per section with a *.md file inside.
```

## 5. Known limitations

- **MinerU table fidelity.** Some DNV design-load and dimensional charts render imperfectly (rows collapsed, symbols mangled). The `_repair_continuation_rows` pass in `pdf_to_latex.py` cleans the most common failure mode but is not exhaustive. RegBench items whose `required_facts` reference these tables were SME-audited; for downstream uses that depend on table fidelity you should spot-check the converter output.
- **Symbol/unit mangling.** Greek letters and superscript units (kN/m², φ, σ) survive correctly in the polished markdown most of the time. The v5d audit explicitly checks for symbol-mangling damage as part of its source-integrity guardrails (see `prompts/v5d_audit.md`, block E1).
- **Edition drift.** DNV reissues the rulebooks periodically. Item ids in the released benchmark are tied to the **2024-07** snapshot; running the converter on a later edition will produce a structurally compatible corpus but section quotes inside `annotator_grounding` may no longer match verbatim.

## What's where

```
pdf_converter/
├── pdf_to_latex.py             # Section-level PDF → markdown driver
├── pdf_to_md_Latex.sh          # Convenience batch runner
├── Magic-PDF/                  # Vendored MinerU (model-source = modelscope)
├── metadata_files/             # ← put DNV PDFs here (intentionally empty)
└── metadata_markdown_files/    # ← converter output lands here (intentionally empty)
```

`Magic-PDF/` is a vendored copy of MinerU. Its upstream is <https://github.com/opendatalab/MinerU>. Licensing follows MinerU's own `LICENSE.md` (AGPL with a CLA), which is preserved in the vendored tree.
