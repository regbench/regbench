"""Minimal RegBench loader.

Two paths:
  1. Hugging Face Hub via `datasets.load_dataset` (preferred).
  2. Local JSONL files shipped in `data/` for offline use.

Usage:
    python examples/load_dataset.py [--source hf|local] [--config dnv|basel|pilot]
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


HF_REPO = "regbench/regbench-release"
LOCAL_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_local(config: str) -> list[dict]:
    path = LOCAL_DATA_DIR / f"{config}.jsonl"
    with path.open() as f:
        return [json.loads(line) for line in f]


def load_hf(config: str):
    from datasets import load_dataset
    return load_dataset(HF_REPO, config, split="test")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["hf", "local"], default="local")
    ap.add_argument("--config", choices=["dnv", "basel", "pilot"], default="dnv")
    args = ap.parse_args()

    items = load_hf(args.config) if args.source == "hf" else load_local(args.config)
    print(f"loaded {len(items)} items from {args.source}:{args.config}")

    item = items[0]
    print(f"\nid:               {item['id']}")
    print(f"tier:             {item['tier']}")
    print(f"source_section:   {item['source_section']}")
    print(f"format:           {item['format']}")
    print(f"question_text[:200]:\n  {item['question_text'][:200]}")
    print(f"required_facts ({len(item['required_facts'])} total):")
    for fact in item["required_facts"][:3]:
        print(f"  - {fact[:140]}{'...' if len(fact) > 140 else ''}")


if __name__ == "__main__":
    main()
