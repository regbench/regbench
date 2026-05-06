#!/usr/bin/env bash
set -euo pipefail

INPUT_DIR="${1:-metadata_files/DNV-RU-SHIP-Pt5}"
OUTPUT_DIR="${2:-metadata_markdown_files/DNV-RU-SHIP-Pt5}"

# INPUT_DIR="${1:-metadata_files/DNV-RU-SHIP-Pt1/DNV-RU-Pt1-Chap1-sec1.pdf}"
# OUTPUT_DIR="${2:-metadata_markdown_files/DNV-RU-SHIP-Pt1/DNV-RU-Pt1-Chap1-sec1}"




# INPUT_DIR="${1:-out_sections/01_Section 1 GENERAL.pdf}"
# OUTPUT_DIR="${2:-demo/DNV-RU-SHIP-Pt5/DNV-RU-Pt5-Chap2-sec1}"

find "$INPUT_DIR" -type f -name '*.pdf' | sort | while IFS= read -r pdf_file; do
  echo "Processing: $pdf_file"
  python pdf_to_latex.py \
    --input_file "$pdf_file" \
    --input_root "$INPUT_DIR" \
    --output_dir "$OUTPUT_DIR" \
    --table_parser text \
    --footer_mask_ratio 0.1
done
