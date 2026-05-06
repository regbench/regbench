#!/usr/bin/env python3
"""
RegBench Second-Domain Cross-Reference Graph Builder — 12 CFR Part 217
======================================================================
Parallel to build_xref_graph.py (DNV), adapted for eCFR Title 12 Part 217
(Federal Reserve Regulation Q, Basel III U.S. implementation).

Structure of the corpus:
  Part 217 -> Subparts A-J -> Sections (e.g., 217.32)
               -> Appendix A to Part 217

Cross-reference types extracted:
  same-section   : "paragraph (c)(1) of this section"             -> intra-section (T1)
  same-subpart   : "§ 217.xx" within same subpart                 -> T2
  cross-subpart  : "§ 217.yy" in different subpart of same Part   -> T3
  cross-part     : "12 CFR part 225" / "12 CFR 225.xx"            -> T4
  subpart-ref    : "subpart B of this part"                       -> coarse cross-subpart (T3)
  appendix-ref   : "appendix A to this part"                      -> treated as separate node

Output: /workspace/regbench_basel/xref_graph.json
"""
import re
import json
from pathlib import Path
from collections import defaultdict, Counter
from xml.etree import ElementTree as ET

RAW_PATH = Path("/workspace/regbench_basel/raw/part217.xml")
OUT_DIR = Path("/workspace/regbench_basel")


def canon(part: str, sec: str = "", subpart: str = "", appendix: str = "") -> str:
    """Canonical node ID. Examples:
      Pt217.SubpartE.Sec217.32  (section)
      Pt217.AppA                (appendix)
    """
    if appendix:
        return f"Pt{part}.App{appendix}"
    if subpart and sec:
        return f"Pt{part}.Subpart{subpart}.Sec{sec}"
    if sec:
        return f"Pt{part}.Sec{sec}"
    return f"Pt{part}"


def strip_tags(elem) -> str:
    """Return all text under an XML element with tags stripped and entities resolved."""
    parts = []

    def walk(e):
        if e.text:
            parts.append(e.text)
        for c in e:
            walk(c)
            if c.tail:
                parts.append(c.tail)

    walk(elem)
    return "".join(parts)


def parse_sections(xml_path: Path):
    """Return list of dicts: {id, part, subpart, sec, head, text}."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    sections = {}

    # Part-level head
    part_n = root.attrib.get("N", "217")

    # Walk subparts
    for subpart_elem in root.findall(".//DIV6[@TYPE='SUBPART']"):
        subpart_n = subpart_elem.attrib.get("N", "?")
        for sec_elem in subpart_elem.findall(".//DIV8[@TYPE='SECTION']"):
            sec_n = sec_elem.attrib.get("N", "?")
            head_elem = sec_elem.find("HEAD")
            head = strip_tags(head_elem).strip() if head_elem is not None else ""
            text = strip_tags(sec_elem).strip()
            node_id = canon(part_n, sec=sec_n, subpart=subpart_n)
            sections[node_id] = {
                "id": node_id,
                "part": part_n,
                "subpart": subpart_n,
                "sec": sec_n,
                "head": head,
                "text": text,
                "chars": len(text),
            }

    # Appendices
    for app_elem in root.findall(".//DIV9[@TYPE='APPENDIX']"):
        app_n = app_elem.attrib.get("N", "?")
        # Keep only last-letter identifier if name is "Appendix A to Part 217"
        m = re.search(r"Appendix\s+([A-Z])", app_n)
        app_letter = m.group(1) if m else app_n[-1]
        head_elem = app_elem.find("HEAD")
        head = strip_tags(head_elem).strip() if head_elem is not None else ""
        text = strip_tags(app_elem).strip()
        node_id = canon(part_n, appendix=app_letter)
        sections[node_id] = {
            "id": node_id,
            "part": part_n,
            "subpart": None,
            "sec": None,
            "appendix": app_letter,
            "head": head,
            "text": text,
            "chars": len(text),
        }

    return sections, part_n


def build_section_index(sections):
    """Map '217.32' -> full node id so we can resolve bare § refs."""
    idx = {}
    for node in sections.values():
        if node.get("sec"):
            idx[node["sec"]] = node["id"]
    return idx


def build_graph(sections, part_n):
    """Extract cross-references and build a directed graph at section granularity."""
    section_idx = build_section_index(sections)
    edges = []
    adjacency = defaultdict(list)

    # Regex patterns (operate on plain text post-tag-strip; § is literal after strip)
    # Note: strip_tags resolves &#xA7; -> § (Python XML parser does this automatically).
    RX_SAME_PART_SECTION = re.compile(
        r"§\s*(?P<sec>217\.\d+[a-z]?)(?P<paras>(?:\([a-z0-9]+\))*)"
    )
    RX_CROSS_PART = re.compile(
        r"12\s+CFR\s+(?:part\s+)?(?P<part>\d+)(?:\.(?P<sec>\d+[a-z]?))?"
    )
    RX_SUBPART_THIS_PART = re.compile(
        r"subpart\s+(?P<subpart>[A-J])\s+of\s+this\s+part", re.IGNORECASE
    )
    RX_APPENDIX_THIS_PART = re.compile(
        r"appendix\s+(?P<app>[A-Z])\s+(?:to|of)\s+(?:this\s+)?part", re.IGNORECASE
    )
    RX_PARAGRAPH_THIS_SECTION = re.compile(
        r"paragraph\s*(?P<paras>(?:\([a-z0-9]+\))+)\s+of\s+this\s+section",
        re.IGNORECASE,
    )

    def add_edge(**kw):
        edges.append(kw)
        adjacency[kw["source"]].append(kw["target"])

    for src_id, s in sections.items():
        text = s["text"]
        src_part = s.get("part")
        src_subpart = s.get("subpart")
        src_sec = s.get("sec")

        # --- Pattern 1: same-Part § 217.xx references ---
        for m in RX_SAME_PART_SECTION.finditer(text):
            tgt_sec_num = m.group("sec")
            tgt_id = section_idx.get(tgt_sec_num)
            if not tgt_id:
                continue
            if tgt_sec_num == src_sec:
                kind = "same_section"
                tier_hint = 1
            else:
                tgt = sections[tgt_id]
                if tgt["subpart"] == src_subpart:
                    kind = "same_subpart"
                    tier_hint = 2
                else:
                    kind = "cross_subpart"
                    tier_hint = 3
            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            add_edge(
                source=src_id,
                target=tgt_id,
                kind=kind,
                tier_hint=tier_hint,
                raw=m.group(0),
                paragraphs=m.group("paras") or "",
                context=text[ctx_start:ctx_end].strip(),
                char_offset=m.start(),
            )

        # --- Pattern 2: cross-Part (other 12 CFR parts) ---
        for m in RX_CROSS_PART.finditer(text):
            tgt_part = m.group("part")
            if tgt_part == src_part:
                continue  # caught by pattern 1 if § prefixed
            tgt_sec_num = m.group("sec")
            if tgt_sec_num:
                tgt_id = f"Pt{tgt_part}.Sec{tgt_part}.{tgt_sec_num}"
            else:
                tgt_id = f"Pt{tgt_part}"
            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            add_edge(
                source=src_id,
                target=tgt_id,
                kind="cross_part",
                tier_hint=4,
                raw=m.group(0),
                paragraphs="",
                context=text[ctx_start:ctx_end].strip(),
                char_offset=m.start(),
            )

        # --- Pattern 3: "subpart X of this part" ---
        for m in RX_SUBPART_THIS_PART.finditer(text):
            tgt_subpart = m.group("subpart")
            if tgt_subpart == src_subpart:
                continue
            # Represent as coarse subpart node (use first section of that subpart as representative,
            # but we'll add a dedicated subpart-collective edge too)
            reps = [
                n["id"]
                for n in sections.values()
                if n.get("subpart") == tgt_subpart and n.get("sec")
            ]
            if not reps:
                continue
            # Use the first section of the target subpart as the representative target
            tgt_id = sorted(reps)[0]
            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            add_edge(
                source=src_id,
                target=tgt_id,
                kind="subpart_rep",
                tier_hint=3,
                raw=m.group(0),
                paragraphs="",
                context=text[ctx_start:ctx_end].strip(),
                char_offset=m.start(),
            )

        # --- Pattern 4: "appendix A to this part" ---
        for m in RX_APPENDIX_THIS_PART.finditer(text):
            tgt_app = m.group("app")
            tgt_id = canon(src_part, appendix=tgt_app)
            if tgt_id not in sections:
                continue
            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            add_edge(
                source=src_id,
                target=tgt_id,
                kind="appendix_ref",
                tier_hint=3,
                raw=m.group(0),
                paragraphs="",
                context=text[ctx_start:ctx_end].strip(),
                char_offset=m.start(),
            )

        # --- Pattern 5: intra-section paragraph refs (T1) ---
        for m in RX_PARAGRAPH_THIS_SECTION.finditer(text):
            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            add_edge(
                source=src_id,
                target=src_id,  # self-loop: intra-section
                kind="same_section_paragraph",
                tier_hint=1,
                raw=m.group(0),
                paragraphs=m.group("paras"),
                context=text[ctx_start:ctx_end].strip(),
                char_offset=m.start(),
            )

    # Deduplicate at (source, target, kind, raw)
    seen = set()
    unique_edges = []
    for e in edges:
        key = (e["source"], e["target"], e["kind"], e["raw"])
        if key not in seen:
            seen.add(key)
            unique_edges.append(e)

    return {
        "edges": unique_edges,
        "nodes": list(sections.keys()),
        "adjacency": {k: list(set(v)) for k, v in adjacency.items()},
    }


def find_chains(graph, sections, max_depth=3):
    edges_by_source = defaultdict(list)
    for e in graph["edges"]:
        # skip self-loops for chain enumeration (those are T1 seeds)
        if e["source"] == e["target"]:
            continue
        edges_by_source[e["source"]].append(e)

    chains = []
    for start in sorted(sections.keys()):
        queue = [(start, [start], [])]
        while queue:
            cur, path, edge_path = queue.pop(0)
            if len(path) > max_depth + 1:
                continue
            for edge in edges_by_source.get(cur, []):
                nxt = edge["target"]
                if nxt in path:
                    continue
                # Keep external target nodes even if not in sections dict (cross-part)
                new_path = path + [nxt]
                new_edges = edge_path + [edge]
                tier = max(e.get("tier_hint", 1) for e in new_edges)
                depth = len(new_edges)
                chains.append(
                    {
                        "start": start,
                        "end": nxt,
                        "path": new_path,
                        "depth": depth,
                        "tier": tier,
                        "kinds": [e["kind"] for e in new_edges],
                        "raws": [e["raw"] for e in new_edges],
                    }
                )
                if depth < max_depth and nxt in sections:
                    queue.append((nxt, new_path, new_edges))
    return chains


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Parsing {RAW_PATH}...")
    sections, part_n = parse_sections(RAW_PATH)
    print(f"  {len(sections)} nodes (sections + appendices)")
    subpart_counts = Counter(s.get("subpart") for s in sections.values())
    print(f"  By subpart: {dict(subpart_counts)}")

    print("\nBuilding cross-reference graph...")
    graph = build_graph(sections, part_n)
    print(f"  {len(graph['edges'])} unique edges")
    kind_counts = Counter(e["kind"] for e in graph["edges"])
    print(f"  By kind: {dict(kind_counts)}")

    # tier_hint distribution
    th_counts = Counter(e["tier_hint"] for e in graph["edges"])
    print(f"  By tier_hint: {dict(sorted(th_counts.items()))}")

    print("\nEnumerating chains (max depth 3)...")
    chains = find_chains(graph, sections, max_depth=3)
    print(f"  {len(chains)} total chains")
    tier_counts = Counter(c["tier"] for c in chains)
    depth_counts = Counter(c["depth"] for c in chains)
    print(f"  By tier: {dict(sorted(tier_counts.items()))}")
    print(f"  By depth: {dict(sorted(depth_counts.items()))}")

    # Example chains
    for tier in [2, 3, 4]:
        tc = [c for c in chains if c["tier"] == tier]
        print(f"\n  === Tier {tier}: {len(tc)} chains ===")
        for c in tc[:3]:
            path_str = " -> ".join(c["path"])
            print(f"    [{path_str}] depth={c['depth']}")
            print(f"      refs: {c['raws']}")

    # Save
    out_graph = OUT_DIR / "xref_graph.json"
    out_sections = OUT_DIR / "sections.json"
    out_graph.write_text(
        json.dumps(
            {
                "sections": {
                    k: {
                        "part": v.get("part"),
                        "subpart": v.get("subpart"),
                        "sec": v.get("sec"),
                        "appendix": v.get("appendix"),
                        "head": v.get("head"),
                        "chars": v.get("chars"),
                    }
                    for k, v in sections.items()
                },
                "edges": graph["edges"],
                "chains": chains,
                "stats": {
                    "total_sections": len(sections),
                    "total_edges": len(graph["edges"]),
                    "total_chains": len(chains),
                    "edges_by_kind": dict(kind_counts),
                    "chains_by_tier": dict(sorted(tier_counts.items())),
                    "chains_by_depth": dict(sorted(depth_counts.items())),
                },
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    out_sections.write_text(
        json.dumps(
            {k: {**v, "text": v.get("text", "")} for k, v in sections.items()},
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"\nSaved {out_graph}")
    print(f"Saved {out_sections}")


if __name__ == "__main__":
    main()
