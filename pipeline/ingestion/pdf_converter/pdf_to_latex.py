import os

# 在脚本开头设置环境变量
os.environ['MINERU_MODEL_SOURCE'] = 'modelscope'

from typing import List, Tuple

import re
import argparse
import json
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
LOCAL_MINERU_ROOT = CURRENT_DIR / "Magic-PDF"
if LOCAL_MINERU_ROOT.exists():
    sys.path.insert(0, str(LOCAL_MINERU_ROOT))

from mineru.data.data_reader_writer.filebase import FileBasedDataWriter, FileBasedDataReader
from mineru.backend.pipeline.pipeline_analyze import doc_analyze as pipeline_doc_analyze
from mineru.backend.pipeline.model_json_to_middle_json import result_to_middle_json
from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make as pipeline_union_make
from mineru.utils.enum_class import MakeMode
def _normalize_cell_text(value):
    if value is None:
        return ""
    value = str(value).replace('\xa0', ' ')
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def _get_pdf_page_sizes(input_file):
    try:
        from pypdf import PdfReader
    except Exception:
        return []
    sizes = []
    try:
        reader = PdfReader(input_file)
        for page in reader.pages:
            box = page.mediabox
            w = float(box.width)
            h = float(box.height)
            sizes.append((w, h))
    except Exception:
        return []
    return sizes


def _repair_continuation_rows(table):
    rows = [[_normalize_cell_text(cell) for cell in (row or [])] for row in (table or [])]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return []

    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    if ncols < 2:
        return rows

    def is_symbol_only(text):
        t = _normalize_cell_text(text)
        if not t:
            return True
        return re.fullmatch(r"[-–—|:;.,_~`]+", t) is not None

    def looks_like_new_block(text):
        t = _normalize_cell_text(text).lower()
        if not t:
            return False
        # Avoid swallowing next table/header/body blocks.
        if re.search(r"\b(table\s+\d+|class notation|description|application|rule reference|term|definition)\b", t):
            return True
        if re.match(r"^\d+(\.\d+)+\b", t):
            return True
        if t.startswith("section ") or t.startswith("chapter "):
            return True
        return False

    repaired = [rows[0]]
    for row in rows[1:]:
        first_non_empty = _normalize_cell_text(row[0])
        rest_parts = [_normalize_cell_text(c) for c in row[1:] if _normalize_cell_text(c)]
        rest_text = " ".join(rest_parts).strip()
        prev = repaired[-1]
        prev_first = _normalize_cell_text(prev[0]) if prev else ""
        # Merge only when current row clearly looks like a continuation and previous row has a real key term.
        if is_symbol_only(first_non_empty) and rest_text and prev_first and not looks_like_new_block(rest_text):
            prev = repaired[-1]
            prev[1] = (prev[1] + " " + rest_text).strip() if prev[1] else rest_text
            continue
        repaired.append(row)
    return repaired


def _looks_like_term_definition_table(rows):
    if not rows:
        return False
    ncols = max(len(r) for r in rows)
    padded = [r + [""] * (ncols - len(r)) for r in rows]

    c0 = _normalize_cell_text(padded[0][0]).lower() if ncols >= 1 else ""
    c1 = _normalize_cell_text(padded[0][1]).lower() if ncols >= 2 else ""
    if c0 != "term" or c1 != "definition":
        return False

    # For genuine term-definition tables, trailing header columns should be empty/symbol-only.
    trailing_header = " ".join(_normalize_cell_text(c) for c in padded[0][2:]).strip()
    if trailing_header and re.search(r"[A-Za-z0-9]", trailing_header):
        return False

    data = padded[1:]
    if not data:
        return False

    # If many rows actually use columns 3+, this is likely NOT a 2-column term-definition table.
    extra_used = sum(1 for r in data if any(_normalize_cell_text(c) for c in r[2:]))
    if extra_used > max(1, int(len(data) * 0.25)):
        return False

    # Require enough rows with both term and definition text.
    td_rows = sum(1 for r in data if _normalize_cell_text(r[0]) and _normalize_cell_text(r[1]))
    return td_rows >= max(2, int(len(data) * 0.4))


def _is_sentence_start_fragment(text):
    t = _normalize_cell_text(text)
    if not t:
        return False
    if re.match(r"^(a|an|the|class|assignment|verbal)\b", t, flags=re.IGNORECASE):
        return True
    return bool(re.match(r"^[A-Z]", t))


def _merge_definition_fragments(fragments):
    parts = [_normalize_cell_text(x) for x in fragments if _normalize_cell_text(x)]
    if not parts:
        return ""
    starts = [p for p in parts if _is_sentence_start_fragment(p)]
    tails = [p for p in parts if p not in starts]
    merged = starts + tails
    return " ".join(merged).strip()


def _collapse_term_definition_columns(rows):
    if not rows:
        return rows
    ncols = max(len(r) for r in rows)
    if ncols <= 2:
        return rows

    padded = [r + [""] * (ncols - len(r)) for r in rows]
    if not _looks_like_term_definition_table(padded):
        return rows

    collapsed = []
    collapsed.append(["Term", "Definition"])
    for row in padded[1:]:
        term = _normalize_cell_text(row[0])
        definition = _merge_definition_fragments(row[1:])
        collapsed.append([term, definition])
    return collapsed


def _table_to_markdown(table):
    rows = _repair_continuation_rows(table)
    rows = _collapse_term_definition_columns(rows)
    if not rows:
        return ""

    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    header = rows[0]
    body = rows[1:] if len(rows) > 1 else []
    sep = ["---"] * ncols

    def row_to_line(r):
        return "| " + " | ".join(r) + " |"

    lines = [row_to_line(header), row_to_line(sep)]
    lines.extend(row_to_line(r) for r in body)
    return "\n".join(lines)


def _count_non_empty_cells(table):
    return sum(1 for row in (table or []) for cell in (row or []) if _normalize_cell_text(cell))


def _table_shape(table):
    rows = [row or [] for row in (table or [])]
    nrows = len(rows)
    ncols = max((len(r) for r in rows), default=0)
    return nrows, ncols


def _is_structured_table(table):
    nrows, ncols = _table_shape(table)
    if nrows < 2:
        return False, f"too_few_rows={nrows}"
    if ncols < 2:
        return False, f"too_few_cols={ncols}"

    nrows, ncols = _table_shape(table)
    if nrows < 2:
        return False, f"too_few_rows={nrows}"

    non_empty = _count_non_empty_cells(table)
    if non_empty < 4:
        return False, f"too_few_non_empty_cells={non_empty}"

    row_non_empty = [sum(1 for c in (r or []) if _normalize_cell_text(c)) for r in (table or [])]
    meaningful_rows = sum(1 for c in row_non_empty if c >= 2)
    if meaningful_rows < 2:
        return False, f"too_few_meaningful_rows={meaningful_rows}"

    return True, ""


def _is_low_quality_table(table):
    cells = [_normalize_cell_text(c) for r in (table or []) for c in (r or [])]
    non_empty = [c for c in cells if c]
    if not non_empty:
        return True, "empty_table"

    nrows, ncols = _table_shape(table)
    short_cells = sum(1 for c in non_empty if len(c) <= 2)
    alpha_cells = sum(1 for c in non_empty if re.search(r"[A-Za-z]", c))
    single_alpha_cells = sum(1 for c in non_empty if re.fullmatch(r"[A-Za-z]", c))

    short_ratio = short_cells / len(non_empty)
    single_alpha_ratio = (single_alpha_cells / alpha_cells) if alpha_cells else 0.0

    # Heuristic: large-grid pages with many tiny/single-letter cells are usually fragmented text,
    # not true semantic tables (common for chart-like pages and footer/header bleed-through).
    if ncols >= 8 and short_ratio > 0.55:
        return True, f"fragmented_grid_short_ratio={short_ratio:.2f},shape={nrows}x{ncols}"
    if ncols >= 8 and single_alpha_ratio > 0.35:
        return True, f"fragmented_grid_single_alpha_ratio={single_alpha_ratio:.2f},shape={nrows}x{ncols}"
    return False, ""


def _pick_best_tables_from_candidates(page_idx, candidates):
    best_tables = []
    best_name = "none"
    best_score = (-1, -1)  # (table_count, non_empty_cells)
    for name, raw_tables in candidates:
        tables = []
        for t_idx, table in enumerate(raw_tables, start=1):
            ok_struct, struct_reason = _is_structured_table(table)
            if not ok_struct:
                print(
                    f"[camelot][page={page_idx}][candidate={name}][table={t_idx}] "
                    f"skip_non_table reason={struct_reason}"
                )
                continue
            tables.append(table)
        cell_count = sum(_count_non_empty_cells(t) for t in tables)
        score = (len(tables), cell_count)
        print(
            f"[camelot][page={page_idx}] "
            f"strategy={name} tables={len(tables)} non_empty_cells={cell_count}"
        )
        if score > best_score:
            best_score = score
            best_tables = tables
            best_name = name
    print(f"[camelot][page={page_idx}] selected_strategy={best_name} score={best_score}")
    return best_tables, best_name, best_score


def extract_tables_with_camelot(input_file, footer_mask_ratio=0.12):
    try:
        import camelot
    except Exception:
        print("[camelot] not installed or import failed, skip table extraction")
        return []

    print(f"[camelot] start table extraction: {input_file}")
    strategy_defs = [
        ("lattice", {"flavor": "lattice"}),
        ("stream", {"flavor": "stream", "row_tol": 10}),
    ]
    page_sizes = _get_pdf_page_sizes(input_file)
    if not page_sizes:
        print("[camelot] cannot read page sizes; fallback to full-page extraction")
    else:
        print(f"[camelot] footer_mask_ratio={footer_mask_ratio}")

    strategy_page_tables = {name: {} for name, _ in strategy_defs}
    all_pages = set()
    page_count = len(page_sizes) if page_sizes else 0

    if page_count == 0:
        # Keep behavior if page size probing fails.
        for strategy_name, options in strategy_defs:
            try:
                tables = camelot.read_pdf(input_file, pages="all", suppress_stdout=True, **options)
            except Exception as e:
                print(f"[camelot] strategy={strategy_name} failed: {e}")
                tables = []
            for t in tables:
                try:
                    page_num = int(t.page)
                except Exception:
                    continue
                rows = t.df.values.tolist()
                strategy_page_tables[strategy_name].setdefault(page_num, []).append(rows)
                all_pages.add(page_num)
    else:
        for page_idx, (w, h) in enumerate(page_sizes, start=1):
            y_bottom = h * max(0.0, min(0.8, float(footer_mask_ratio)))
            area = f"0,{h},{w},{y_bottom}"
            for strategy_name, options in strategy_defs:
                try:
                    tables = camelot.read_pdf(
                        input_file,
                        pages=str(page_idx),
                        table_areas=[area],
                        suppress_stdout=True,
                        **options,
                    )
                except Exception as e:
                    print(f"[camelot] strategy={strategy_name} page={page_idx} failed: {e}")
                    tables = []
                rows_list = [t.df.values.tolist() for t in tables]
                strategy_page_tables[strategy_name][page_idx] = rows_list
                if rows_list:
                    all_pages.add(page_idx)

    if not all_pages:
        print("[camelot] no tables detected")
        return []

    print(f"[camelot] pages_with_tables={len(all_pages)}")
    extracted = []
    for page_idx in sorted(all_pages):
        candidates = []
        for strategy_name, _ in strategy_defs:
            candidates.append((strategy_name, strategy_page_tables.get(strategy_name, {}).get(page_idx, [])))
        tables, strategy_name, score = _pick_best_tables_from_candidates(page_idx, candidates)
        print(f"[camelot][page={page_idx}] using={strategy_name} tables={len(tables)} score={score}")
        for table_idx, table in enumerate(tables, start=1):
            is_bad, reason = _is_low_quality_table(table)
            if is_bad:
                nrows, ncols = _table_shape(table)
                print(
                    f"[camelot][page={page_idx}][table={table_idx}] "
                    f"skip_low_quality reason={reason} shape={nrows}x{ncols}"
                )
                continue
            md = _table_to_markdown(table)
            if not md:
                continue
            extracted.append(
                {
                    "page": page_idx,
                    "table_index": table_idx,
                    "markdown": md,
                }
            )
    print(f"[camelot] done. extracted_tables={len(extracted)}")
    return extracted


def _find_markdown_table_blocks(md_text: str) -> List[Tuple[int, int]]:
    lines = md_text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if "|" not in lines[i]:
            i += 1
            continue
        start = i
        j = i
        while j < len(lines) and "|" in lines[j]:
            j += 1
        if j - start >= 2:
            blocks.append((start, j))
        i = j
    return blocks


def _find_html_table_blocks(md_text: str) -> List[Tuple[int, int]]:
    lines = md_text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        line_lower = lines[i].lower()
        if "<table" not in line_lower:
            i += 1
            continue

        start = i
        j = i
        found_end = "</table>" in line_lower
        while j + 1 < len(lines) and not found_end:
            j += 1
            if "</table>" in lines[j].lower():
                found_end = True

        if found_end:
            blocks.append((start, j + 1))
            i = j + 1
        else:
            # broken html table block, do not replace
            i += 1
    return blocks


def _normalize_for_match(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _token_set(text: str):
    return {t for t in _normalize_for_match(text).split(" ") if len(t) >= 3}


def _similarity(a: str, b: str) -> float:
    ta = _token_set(a)
    tb = _token_set(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    denom = min(len(ta), len(tb))
    return inter / denom if denom else 0.0


def _is_noisy_block(text: str) -> bool:
    t = _normalize_cell_text(text).lower()
    if not t:
        return False
    if re.search(r"\bpage\s*\d+\b", t) and re.search(r"\bdnv as\b", t):
        return True
    if re.search(r"\bclass notation\b", t) and re.search(r"\bapplication\b", t) and re.search(r"\brule reference\b", t):
        return True
    if "5tr" in t or "shiswththenotation" in t:
        return True
    return False


def _dedupe_table_entries(table_entries, similarity_threshold=0.92):
    deduped = []
    for entry in table_entries:
        md = entry.get("markdown", "")
        if not md.strip():
            continue
        is_dup = False
        for kept in deduped:
            if _similarity(md, kept.get("markdown", "")) >= similarity_threshold:
                is_dup = True
                break
        if not is_dup:
            deduped.append(entry)
    removed = len(table_entries) - len(deduped)
    if removed:
        print(f"[table_dedupe] removed_duplicates={removed} kept={len(deduped)}")
    return deduped


def replace_markdown_tables(md_text, table_entries):
    if not table_entries:
        return md_text, 0, 0

    table_entries = _dedupe_table_entries(table_entries)

    lines = md_text.splitlines()
    md_blocks = _find_markdown_table_blocks(md_text)
    html_blocks = _find_html_table_blocks(md_text)
    blocks = sorted(md_blocks + html_blocks, key=lambda x: x[0])
    print(
        f"[table_replace] markdown_blocks={len(md_blocks)} html_blocks={len(html_blocks)} total_blocks={len(blocks)}"
    )
    if not blocks:
        appended = "\n\n".join(entry["markdown"] for entry in table_entries if entry.get("markdown"))
        if not appended:
            return md_text, 0, 0
        print(f"[table_replace] no target blocks found, append_all={len(table_entries)}")
        merged = md_text.rstrip() + "\n\n" + appended + "\n"
        return merged, 0, len(table_entries)

    block_texts = ["\n".join(lines[s:e]) for s, e in blocks]
    # table_idx -> block_idx
    matched_pairs = {}
    used_blocks = set()
    base_threshold = 0.60
    for t_idx, entry in enumerate(table_entries):
        t_text = entry.get("markdown", "")
        best_b = None
        best_score = 0.0
        for b_idx, b_text in enumerate(block_texts):
            if b_idx in used_blocks:
                continue
            score = _similarity(t_text, b_text)
            if score > best_score:
                best_score = score
                best_b = b_idx
        chosen_threshold = 0.45 if (best_b is not None and _is_noisy_block(block_texts[best_b])) else base_threshold
        if best_b is not None and best_score >= chosen_threshold:
            matched_pairs[t_idx] = best_b
            used_blocks.add(best_b)
            print(
                f"[table_replace] match table={t_idx + 1} -> block={best_b + 1} "
                f"score={best_score:.3f} threshold={chosen_threshold:.2f}"
            )
        else:
            print(
                f"[table_replace] no_match table={t_idx + 1} "
                f"best_score={best_score:.3f} threshold={chosen_threshold:.2f}"
            )

    block_to_table = {b: t for t, b in matched_pairs.items()}
    replace_count = len(block_to_table)
    unmatched_tables = [idx for idx in range(len(table_entries)) if idx not in matched_pairs]
    # Avoid appending only near-exact duplicates already present in existing table-like blocks.
    # Keep this strict so unmatched tables are still visible in output.
    filtered_unmatched = []
    for t_idx in unmatched_tables:
        t_md = table_entries[t_idx]["markdown"]
        best_block_sim = 0.0
        for b_text in block_texts:
            sim = _similarity(t_md, b_text)
            if sim > best_block_sim:
                best_block_sim = sim
        # if best_block_sim >= 0.90:
        #     print(
        #         f"[table_replace] skip_append table={t_idx + 1} already_present_sim={best_block_sim:.3f}"
        #     )
        #     continue
        filtered_unmatched.append(t_idx)
    unmatched_tables = filtered_unmatched
    appended_count = len(unmatched_tables)
    print(
        f"[table_replace] tables_from_camelot={len(table_entries)} replacing={replace_count} appending={appended_count}"
    )

    last_matched_block = max(block_to_table.keys()) if block_to_table else None
    result = []
    cursor = 0
    for b_idx, (start, end) in enumerate(blocks):
        result.extend(lines[cursor:start])
        if b_idx in block_to_table:
            t_idx = block_to_table[b_idx]
            result.append(table_entries[t_idx]["markdown"])
        else:
            result.extend(lines[start:end])
        if b_idx == last_matched_block and unmatched_tables:
            for t_idx in unmatched_tables:
                result.append(table_entries[t_idx]["markdown"])
        cursor = end
    result.extend(lines[cursor:])

    if last_matched_block is None and unmatched_tables:
        # No internal match at all: append to tail as final fallback.
        for t_idx in unmatched_tables:
            result.append(table_entries[t_idx]["markdown"])

    return "\n".join(result).strip() + "\n", replace_count, appended_count


def _html_table_to_plain_text(table_html: str) -> str:
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL)
    out_lines = []
    for row_html in rows:
        cells = re.findall(
            r"<t[dh]\b[^>]*>(.*?)</t[dh]>",
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        cleaned_cells = []
        for cell in cells:
            text = re.sub(r"<[^>]+>", " ", cell)
            text = _normalize_cell_text(text)
            if text:
                cleaned_cells.append(text)
        if cleaned_cells:
            out_lines.append(" | ".join(cleaned_cells))
    return "\n".join(out_lines).strip()


def convert_html_tables_to_text(md_text: str) -> Tuple[str, int]:
    pattern = re.compile(r"<table\b[^>]*>.*?</table>", flags=re.IGNORECASE | re.DOTALL)
    converted = 0

    def repl(match):
        nonlocal converted
        plain = _html_table_to_plain_text(match.group(0))
        if not plain:
            return match.group(0)
        converted += 1
        return "\n" + plain + "\n"

    result = pattern.sub(repl, md_text)
    return result, converted


def postprocess_md(md_text):
    lines = md_text.splitlines()
    rebuilt = []
    i = 0

    def is_condition_line(text):
        normalized = re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip().lower()
        return normalized.startswith("for hatchway corners")

    def extract_array_rows(equation_body):
        body = re.sub(r'\\begin\{array\}\s*\{[^}]*\}', '', equation_body)
        body = re.sub(r'\\end\{array\}', '', body)
        rows = []
        for raw_row in re.split(r'\\\\', body):
            row = raw_row.strip()
            if not row:
                continue
            row = re.sub(r'^\s*&\s*', '', row)
            row = re.sub(r'^\{\s*', '', row)
            row = re.sub(r'\s*\}\s*$', '', row)
            row = row.strip()
            if row:
                rows.append(row)
        return rows

    while i < len(lines):
        line = lines[i]
        if line.strip() != "$$":
            rebuilt.append(line)
            i += 1
            continue

        j = i + 1
        equation_lines = []
        while j < len(lines) and lines[j].strip() != "$$":
            equation_lines.append(lines[j])
            j += 1

        if j >= len(lines):
            rebuilt.append(line)
            rebuilt.extend(equation_lines)
            break

        equation_body = "\n".join(equation_lines).strip()
        row_matches = extract_array_rows(equation_body)

        prev_idx = len(rebuilt) - 1
        while prev_idx >= 0 and not rebuilt[prev_idx].strip():
            prev_idx -= 1
        previous_condition = rebuilt[prev_idx] if prev_idx >= 0 and is_condition_line(rebuilt[prev_idx]) else None

        next_conditions = []
        k = j + 1
        while k < len(lines):
            candidate = lines[k].strip()
            if not candidate:
                k += 1
                continue
            if is_condition_line(lines[k]):
                next_conditions.append(lines[k])
                k += 1
                continue
            break

        if (
            "\\begin{array}" in equation_body
            and previous_condition
            and row_matches
            and len(row_matches) == 1 + len(next_conditions)
        ):
            rebuilt.pop(prev_idx)
            condition_lines = [previous_condition] + next_conditions
            for idx, row in enumerate(row_matches):
                rebuilt.append(condition_lines[idx])
                rebuilt.append("")
                rebuilt.append("$$")
                rebuilt.append(row.strip())
                rebuilt.append("$$")
                rebuilt.append("")
            i = k
            continue

        rebuilt.append(line)
        rebuilt.extend(equation_lines)
        rebuilt.append(lines[j])
        i = j + 1

    md_text = "\n".join(rebuilt)
    md_text = re.sub(r'(?m)^(for hatchway corners.*HC3\s*)\n(in Figure \d+)\s*$', r'\1 \2', md_text)
    md_text = re.sub(r'\n{3,}', '\n\n', md_text).strip() + "\n"
    return md_text


def prepare_env(output_dir, pdf_name, relative_parent=""):
    name_without_suff = os.path.splitext(os.path.basename(pdf_name))[0]
    base_output_dir = os.path.abspath(output_dir)
    if relative_parent:
        base_output_dir = os.path.join(base_output_dir, relative_parent)
    local_md_dir = os.path.join(base_output_dir, name_without_suff, "pipeline")
    local_image_dir = os.path.join(local_md_dir, "images")
    os.makedirs(local_md_dir, exist_ok=True)
    os.makedirs(local_image_dir, exist_ok=True)
    return name_without_suff, local_md_dir, local_image_dir


def to_md(input_file, output_dir, input_root=None, table_parser="text", footer_mask_ratio=0.12):
    pdf_file_name = os.path.basename(input_file)
    relative_parent = ""
    if input_root:
        input_root_path = os.path.abspath(input_root)
        input_file_path = os.path.abspath(input_file)
        try:
            rel_path = os.path.relpath(input_file_path, input_root_path)
            rel_parent = os.path.dirname(rel_path)
            if rel_parent and rel_parent != "." and not rel_parent.startswith(".."):
                relative_parent = rel_parent
        except ValueError:
            pass

    name_without_suff, local_md_dir, local_image_dir = prepare_env(output_dir, pdf_file_name, relative_parent)
    image_dir = os.path.basename(local_image_dir)

    print(f"local_image_dir {local_image_dir} local_md_dir {local_md_dir}")

    image_writer, md_writer = FileBasedDataWriter(local_image_dir), FileBasedDataWriter(local_md_dir)
    reader1 = FileBasedDataReader("")
    pdf_bytes = reader1.read(input_file)

    infer_results, all_image_lists, all_pdf_docs, lang_list, ocr_enabled_list = pipeline_doc_analyze(
        [pdf_bytes],
        [None],
        parse_method="auto",
        formula_enable=True,
        table_enable=True,
    )

    model_list = infer_results[0]
    images_list = all_image_lists[0]
    pdf_doc = all_pdf_docs[0]
    lang = lang_list[0]
    ocr_enabled = ocr_enabled_list[0]

    middle_json = result_to_middle_json(
        model_list,
        images_list,
        pdf_doc,
        image_writer,
        lang,
        ocr_enabled,
        True,
    )

    pdf_info = middle_json["pdf_info"]
    md_content = pipeline_union_make(pdf_info, MakeMode.MM_MD, image_dir)
    content_list = pipeline_union_make(pdf_info, MakeMode.CONTENT_LIST, image_dir)

    raw_md_name = f"{name_without_suff}_raw.md"
    md_writer.write_string(raw_md_name, md_content)

    processed_md_content = postprocess_md(md_content)
    table_entries = []
    replaced_tables = 0
    appended_tables = 0
    if table_parser in {"text", "auto"}:
        table_entries = extract_tables_with_camelot(input_file, footer_mask_ratio=footer_mask_ratio)
        processed_md_content, replaced_tables, appended_tables = replace_markdown_tables(processed_md_content, table_entries)
    processed_md_content, html_tables_converted = convert_html_tables_to_text(processed_md_content)
    if html_tables_converted:
        print(f"[table_fallback] converted_html_tables_to_text={html_tables_converted}")

    md_writer.write_string(f"{name_without_suff}.md", processed_md_content)
    md_writer.write_string(
        f"{name_without_suff}_content_list.json",
        json.dumps(content_list, ensure_ascii=False, indent=4),
    )
    md_writer.write_string(
        f"{name_without_suff}_middle.json",
        json.dumps(middle_json, ensure_ascii=False, indent=4),
    )
    md_writer.write_string(
        f"{name_without_suff}_model.json",
        json.dumps(model_list, ensure_ascii=False, indent=4),
    )
    md_writer.write_string(
        f"{name_without_suff}_tables_debug.json",
        json.dumps(
            {
                "table_parser": table_parser,
                "footer_mask_ratio": footer_mask_ratio,
                "tables_from_camelot": len(table_entries),
                "tables_replaced_in_markdown": replaced_tables,
                "tables_appended_after_last_match": appended_tables,
                "html_tables_converted_to_text": html_tables_converted,
                "tables": table_entries,
            },
            ensure_ascii=False,
            indent=4,
        ),
    )

    print(f"Markdown generated: {os.path.join(local_md_dir, f'{name_without_suff}.md')}")
    print(f"Raw markdown backup: {os.path.join(local_md_dir, raw_md_name)}")
        
def main():
    parser = argparse.ArgumentParser(description="Convert documents to Markdown format.")
    parser.add_argument("--output_dir", type=str, default="output", help="Directory to save output files")
    parser.add_argument("--input_file", type=str, required=True, help="Path to the input file")
    parser.add_argument(
        "--input_root",
        type=str,
        default=None,
        help="Input root directory. Relative subfolders under this root are mirrored in output_dir.",
    )
    parser.add_argument(
        "--table_parser",
        type=str,
        choices=["none", "text", "auto"],
        default="text",
        help="Table extraction mode. 'text' uses camelot and replaces markdown/html tables when possible.",
    )
    parser.add_argument(
        "--footer_mask_ratio",
        type=float,
        default=0.12,
        help="Bottom area ratio to ignore before Camelot extraction (0 disables, e.g. 0.12 means mask bottom 12%).",
    )

    args = parser.parse_args()
    
    to_md(
        args.input_file,
        args.output_dir,
        args.input_root,
        table_parser=args.table_parser,
        footer_mask_ratio=args.footer_mask_ratio,
    )

if __name__ == "__main__":
    main()
