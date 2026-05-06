#!/usr/bin/env python3
"""
RegBench Cross-Reference Graph Builder
========================================
Mechanically extracts ALL formal cross-references from DNV regulatory documents
and builds a directed graph. No LLM involved.

Each node = a clause (e.g., "Pt5.Ch2.Sec3.[2.2.3]")
Each edge = a formal cross-reference pointer in the source text

Then enumerates all chains by depth for tier assignment:
  depth 0: answer within the clause itself (T0)
  depth 1: answer requires one hop within same section (T1)
  depth 1 cross-section: answer requires one hop to another section (T2)
  depth 2+: multi-hop chains (T3+)
  cross-part: any hop crossing Part boundaries (T4)
"""
import re, json
from pathlib import Path
from collections import defaultdict, Counter

PT5_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt5")
PT3_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt3")
PT1_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt1")


def parse_section_identity(dir_name: str) -> dict | None:
    """Parse 'DNV-RU-Pt5-Chap2-sec3' into structured identity."""
    m = re.match(r'DNV-RU-Pt(\d+)-Chap(\d+)-sec(\d+)', dir_name)
    if not m:
        return None
    return {"pt": int(m.group(1)), "ch": int(m.group(2)), "sec": int(m.group(3))}


def canon(pt: int, ch: int, sec: int, clause: str = "") -> str:
    """Canonical node ID: 'Pt5.Ch2.Sec3' or 'Pt5.Ch2.Sec3.[2.2.3]'"""
    base = f"Pt{pt}.Ch{ch}.Sec{sec}"
    if clause:
        return f"{base}.[{clause}]"
    return base


def load_all_sections() -> dict[str, dict]:
    """Load all section texts. Returns {canon_id: {text, identity, path}}."""
    sections = {}
    for base_dir in [PT5_DIR, PT3_DIR, PT1_DIR]:
        if not base_dir.exists():
            continue
        for sec_dir in sorted(base_dir.iterdir()):
            ident = parse_section_identity(sec_dir.name)
            if not ident:
                continue
            pipeline = sec_dir / "pipeline"
            if not pipeline.exists():
                continue
            md_files = [f for f in pipeline.glob("*.md")
                        if "_raw" not in f.name and "_content" not in f.name]
            if not md_files:
                continue
            text = md_files[0].read_text(errors="replace")
            cid = canon(ident["pt"], ident["ch"], ident["sec"])
            sections[cid] = {
                "text": text,
                "identity": ident,
                "path": str(md_files[0]),
                "chars": len(text),
                "dir_name": sec_dir.name,
            }
    return sections


def extract_clause_context(text: str, clause: str, window: int = 500) -> str:
    """Extract the text surrounding a specific clause reference."""
    pattern = re.escape(f"[{clause}]")
    m = re.search(pattern, text)
    if not m:
        # Try without brackets
        m = re.search(re.escape(clause), text)
    if not m:
        return ""
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    return text[start:end]


def build_graph(sections: dict) -> dict:
    """
    Build directed graph of cross-references.
    Returns {edges: [...], nodes: [...], adjacency: {node: [targets]}}
    """
    edges = []
    adjacency = defaultdict(list)

    for src_id, src_data in sections.items():
        text = src_data["text"]
        src_ident = src_data["identity"]

        # Pattern 1: Full path "Pt.X Ch.Y Sec.Z [clause]"
        for m in re.finditer(r'Pt\.?\s*(\d+)\s+Ch\.?\s*(\d+)\s+Sec\.?\s*(\d+)\s*\[([^\]]+)\]', text):
            tgt_pt, tgt_ch, tgt_sec = int(m.group(1)), int(m.group(2)), int(m.group(3))
            clause = m.group(4)
            tgt_id = canon(tgt_pt, tgt_ch, tgt_sec, clause)
            tgt_section = canon(tgt_pt, tgt_ch, tgt_sec)

            crosses_part = tgt_pt != src_ident["pt"]
            crosses_chapter = tgt_ch != src_ident["ch"] and not crosses_part
            crosses_section = tgt_sec != src_ident["sec"] and not crosses_chapter and not crosses_part

            # Get surrounding context
            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            context = text[ctx_start:ctx_end].strip()

            edge = {
                "source": src_id,
                "target_section": tgt_section,
                "target_clause": tgt_id,
                "clause": clause,
                "raw": m.group(0),
                "crosses_part": crosses_part,
                "crosses_chapter": crosses_chapter,
                "crosses_section": crosses_section,
                "context": context,
                "char_offset": m.start(),
            }
            edges.append(edge)
            adjacency[src_id].append(tgt_section)

        # Pattern 2: "Sec.Z [clause]" (same part+chapter)
        for m in re.finditer(r'Sec\.?\s*(\d+)\s*\[([^\]]+)\]', text):
            pre = text[max(0, m.start()-30):m.start()]
            if re.search(r'Ch\.?\s*\d+\s*$', pre):
                continue  # Part of full_path match (Ch.X Sec.Y)

            tgt_sec = int(m.group(1))
            clause = m.group(2)
            if tgt_sec == src_ident["sec"]:
                continue  # Same section = T1, not cross-section

            tgt_id = canon(src_ident["pt"], src_ident["ch"], tgt_sec, clause)
            tgt_section = canon(src_ident["pt"], src_ident["ch"], tgt_sec)

            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            context = text[ctx_start:ctx_end].strip()

            edge = {
                "source": src_id,
                "target_section": tgt_section,
                "target_clause": tgt_id,
                "clause": clause,
                "raw": m.group(0),
                "crosses_part": False,
                "crosses_chapter": False,
                "crosses_section": True,
                "context": context,
                "char_offset": m.start(),
            }
            edges.append(edge)
            adjacency[src_id].append(tgt_section)

        # Pattern 3: "Ch.X Sec.Y [clause]" — cross-chapter within same part (NO Pt prefix)
        for m in re.finditer(r'(?<!Pt\.\s)(?<!Pt\. )Ch\.?\s*(\d+)\s+Sec\.?\s*(\d+)\s*(?:\[([^\]]+)\])?', text):
            # Skip if preceded by "Pt.X" (that's pattern 1)
            pre = text[max(0, m.start()-15):m.start()]
            if re.search(r'Pt\.?\s*\d+\s*$', pre):
                continue

            tgt_ch = int(m.group(1))
            tgt_sec = int(m.group(2))
            clause = m.group(3) or ""

            if tgt_ch == src_ident["ch"]:
                continue  # Same chapter = pattern 2 territory

            tgt_id = canon(src_ident["pt"], tgt_ch, tgt_sec, clause)
            tgt_section = canon(src_ident["pt"], tgt_ch, tgt_sec)

            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(text), m.end() + 200)
            context = text[ctx_start:ctx_end].strip()

            edge = {
                "source": src_id,
                "target_section": tgt_section,
                "target_clause": tgt_id,
                "clause": clause,
                "raw": m.group(0),
                "crosses_part": False,
                "crosses_chapter": True,
                "crosses_section": False,
                "context": context,
                "char_offset": m.start(),
            }
            edges.append(edge)
            adjacency[src_id].append(tgt_section)

    # Deduplicate edges
    seen = set()
    unique_edges = []
    for e in edges:
        key = (e["source"], e["target_clause"])
        if key not in seen:
            seen.add(key)
            unique_edges.append(e)

    return {
        "edges": unique_edges,
        "nodes": list(sections.keys()),
        "adjacency": dict(adjacency),
    }


def find_chains(graph: dict, sections: dict, max_depth: int = 4) -> list[dict]:
    """
    BFS/DFS to enumerate all cross-reference chains.
    A chain is a path: start_clause → hop1 → hop2 → ... → endpoint
    """
    adjacency = graph["adjacency"]
    edges_by_source = defaultdict(list)
    for e in graph["edges"]:
        edges_by_source[e["source"]].append(e)

    chains = []

    # Start from ALL sections (not just Pt5.Ch2) — we need T0/T1 starts from any section
    for start_node in sorted(adjacency.keys()):
        # BFS from this node
        queue = [(start_node, [start_node], [])]  # (current, path, edges_used)

        while queue:
            current, path, edge_path = queue.pop(0)

            if len(path) > max_depth + 1:
                continue

            for edge in edges_by_source.get(current, []):
                next_node = edge["target_section"]

                if next_node in set(path):  # Avoid cycles
                    continue

                if next_node not in sections:  # Target section not in our data
                    continue

                new_path = path + [next_node]
                new_edges = edge_path + [edge]

                # Classify this chain
                any_cross_part = any(e.get("crosses_part") for e in new_edges)
                any_cross_chapter = any(e.get("crosses_chapter") for e in new_edges)
                any_cross_section = any(e.get("crosses_section") for e in new_edges)
                depth = len(new_edges)

                # Determine tier (proper definitions)
                # T0 = within one subrule (not findable from this graph — that's intra-clause)
                # T1 = same section, formula/table (depth 0 = no chain)
                # T2 = cross-section, same chapter
                # T3 = cross-chapter, same part
                # T4 = cross-part
                if any_cross_part:
                    tier = 4
                elif any_cross_chapter:
                    tier = 3
                elif any_cross_section:
                    tier = 2
                else:
                    tier = 1  # shouldn't happen since we don't add intra-section edges

                chain = {
                    "start": path[0],
                    "end": next_node,
                    "path": new_path,
                    "depth": depth,
                    "tier": tier,
                    "edges": [{
                        "source": e["source"],
                        "target": e["target_section"],
                        "target_clause": e["target_clause"],
                        "clause": e["clause"],
                        "raw": e["raw"],
                        "crosses_part": e.get("crosses_part", False),
                        "crosses_chapter": e.get("crosses_chapter", False),
                        "crosses_section": e.get("crosses_section", False),
                        "context": e["context"][:200],
                    } for e in new_edges],
                    "cross_part": any_cross_part,
                    "cross_chapter": any_cross_chapter,
                }
                chains.append(chain)

                # Continue BFS for deeper chains
                if depth < max_depth:
                    queue.append((next_node, new_path, new_edges))

    return chains


def main():
    print("Loading sections...")
    sections = load_all_sections()
    print(f"  {len(sections)} sections loaded")
    for prefix in ["Pt1", "Pt3", "Pt5"]:
        count = sum(1 for k in sections if k.startswith(prefix))
        print(f"    {prefix}: {count}")

    print("\nBuilding cross-reference graph...")
    graph = build_graph(sections)
    print(f"  {len(graph['edges'])} unique edges")
    print(f"  {len(graph['nodes'])} nodes")

    # Edge type breakdown
    cross_part = sum(1 for e in graph["edges"] if e["crosses_part"])
    cross_section = sum(1 for e in graph["edges"] if e["crosses_section"])
    print(f"  Cross-part edges: {cross_part}")
    print(f"  Cross-section edges: {cross_section}")

    print("\nEnumerating chains (max depth 3)...")
    chains = find_chains(graph, sections, max_depth=3)
    print(f"  {len(chains)} total chains")

    tier_counts = Counter(c["tier"] for c in chains)
    depth_counts = Counter(c["depth"] for c in chains)
    print(f"\n  By tier: {dict(sorted(tier_counts.items()))}")
    print(f"  By depth: {dict(sorted(depth_counts.items()))}")

    # Show examples
    for tier in [2, 3, 4]:
        tier_chains = [c for c in chains if c["tier"] == tier]
        print(f"\n=== Tier {tier}: {len(tier_chains)} chains ===")
        for c in tier_chains[:3]:
            path_str = " → ".join(c["path"])
            clauses = [e["raw"] for e in c["edges"]]
            print(f"  [{path_str}] depth={c['depth']}")
            print(f"    Refs: {clauses}")

    # Save
    out_path = Path("/workspace/xref_graph.json")
    out = {
        "sections": {k: {"chars": v["chars"], "identity": v["identity"]} for k, v in sections.items()},
        "edges": graph["edges"],
        "chains": chains,
        "stats": {
            "total_sections": len(sections),
            "total_edges": len(graph["edges"]),
            "total_chains": len(chains),
            "chains_by_tier": dict(sorted(tier_counts.items())),
            "chains_by_depth": dict(sorted(depth_counts.items())),
        }
    }
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
