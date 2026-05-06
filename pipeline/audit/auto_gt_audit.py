#!/usr/bin/env python3
"""
Three-module automated GT-audit pipeline for RegBench.

Module 1: Source-integrity audit (Sonnet) — detect MinerU damage.
Module 2: Sonnet canonical pairing — establish reference answer + fact set.
Module 3: GPT-5.4 adversarial validation — independent cross-model verdict.

Runs on a 40-Q validation set (20 SME-3-errored + 20 SME-3-clean)
and computes recall/precision against SME-3 ground truth.
"""
import argparse, json, os, re, subprocess, sys, hashlib
from pathlib import Path
from datetime import datetime

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_client import LLMClient
from gt_audit_prompts import (
    SOURCE_INTEGRITY_PROMPT, SONNET_PAIRING_PROMPT, ADVERSARIAL_VALIDATION_PROMPT,
)


PT1_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt1")
PT3_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt3")
PT5_DIR = Path("/workspace/ocx_explanation/DNV-RU-SHIP-Pt5")


def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def section_path(sec_id: str) -> Path | None:
    m = re.match(r"Pt(\d+)\.Ch(\d+)\.Sec(\d+)", sec_id)
    if not m: return None
    pt, ch, sec = m.group(1), m.group(2), m.group(3)
    base = {"1": PT1_DIR, "3": PT3_DIR, "5": PT5_DIR}.get(pt)
    if not base: return None
    return base / f"DNV-RU-Pt{pt}-Chap{ch}-sec{sec}" / "pipeline"


def load_section_md(sec_id: str) -> str:
    p = section_path(sec_id)
    if not p or not p.exists(): return ""
    for f in p.glob("*.md"):
        if "_raw" not in f.name and "_content" not in f.name:
            return f.read_text(errors="replace")
    return ""


def section_head_from_id(sec_id: str) -> str:
    return sec_id  # could parse actual head later


def strip_json(raw: str) -> str:
    raw = (raw or "").strip()
    if "<think>" in raw:
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    i, j = raw.find("{"), raw.rfind("}")
    return raw[i:j+1] if i >= 0 and j > i else raw


def parse_json_safe(raw: str) -> dict:
    try:
        return json.loads(strip_json(raw))
    except Exception as e:
        return {"_parse_error": str(e), "_raw_head": (raw or "")[:400]}


# ----------- Module 1: source integrity via Sonnet -----------

def run_source_integrity(section_id: str, client: LLMClient, model: str) -> dict:
    text = load_section_md(section_id)
    if not text:
        return {"integrity_status": "missing", "damage_signals": [{"type": "section_missing"}],
                "suspect_content_areas": [], "overall_confidence_weight": 0.0}
    # Truncate enormous sections to keep prompt tractable
    if len(text) > 40000:
        text = text[:40000] + "\n\n[section truncated for integrity audit]"
    prompt = SOURCE_INTEGRITY_PROMPT.format(
        section_id=section_id, section_head=section_head_from_id(section_id),
        section_text=text,
    )
    cache_key = f"gt_audit_integrity_{section_id}_{model}".replace(".", "_")
    resp = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=model, max_tokens=2048, temperature=0.0, cache_key=cache_key,
    )
    return parse_json_safe(resp)


# ----------- Module 2: Sonnet canonical pairing -----------

def format_integrity_notice(integrity_per_section: dict) -> str:
    notes = []
    for sid, info in integrity_per_section.items():
        status = info.get("integrity_status", "unknown")
        weight = info.get("overall_confidence_weight", 0.5)
        if status != "clean":
            signals = info.get("damage_signals", [])
            sig_str = "; ".join(f"{s.get('type','?')}: {s.get('detail','?')[:100]}" for s in signals[:3])
            notes.append(f"- {sid}: {status} (confidence weight {weight:.2f}): {sig_str}")
        else:
            notes.append(f"- {sid}: clean (weight {weight:.2f})")
    return "\n".join(notes) if notes else "All sections rated clean by integrity module."


def run_sonnet_pairing(q: dict, section_texts: dict, integrity_per_section: dict,
                       client: LLMClient, model: str) -> dict:
    facts = q.get("required_facts", [])
    facts_numbered = "\n".join(f"[{i+1}] {f}" for i, f in enumerate(facts))
    bundle_parts = []
    for sid, text in section_texts.items():
        if len(text) > 25000:
            text = text[:25000] + "\n\n[truncated]"
        bundle_parts.append(f"### {sid}\n{text}")
    section_bundle = "\n\n".join(bundle_parts)[:120000]
    prompt = SONNET_PAIRING_PROMPT.format(
        question_text=q["question_text"],
        required_facts_numbered=facts_numbered,
        section_bundle=section_bundle,
        integrity_notice=format_integrity_notice(integrity_per_section),
    )
    cache_key = f"gt_audit_pairing_L2L4_{q['id']}_{model}".replace(".", "_")
    resp = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=model, max_tokens=4096, temperature=0.0, cache_key=cache_key,
    )
    return parse_json_safe(resp)


# ----------- Module 3: GPT-5.4 adversarial validation -----------

def call_gpt54_via_codex(prompt: str, timeout: int = 600) -> str:
    out_file = Path("/tmp/gt_audit_gpt54_out.txt"); out_file.write_text("")
    sandbox = Path("/root/codex_sandbox"); sandbox.mkdir(exist_ok=True)
    for f in sandbox.iterdir():
        try:
            if f.is_file(): f.unlink()
        except: pass
    env = dict(os.environ)
    env["PATH"] = "/usr/local/share/npm-global/bin:/usr/local/bin:/usr/bin:/bin"
    try:
        proc = subprocess.run(
            ["codex", "exec", "-m", "gpt-5.4",
             "-c", "model_reasoning_effort=medium",
             "--ephemeral",
             "-C", str(sandbox), "--skip-git-repo-check",
             "--sandbox", "read-only", "-o", str(out_file)],
            input=prompt, capture_output=True, text=True,
            timeout=timeout, env=env, cwd=str(sandbox),
        )
        out = out_file.read_text().strip()
        return out if out else f"[ERROR: codex rc={proc.returncode} stderr={proc.stderr[:300]}]"
    except subprocess.TimeoutExpired:
        return "[ERROR: codex timeout]"
    except Exception as e:
        return f"[ERROR: {type(e).__name__}: {e}]"


def run_gpt54_adversarial(q: dict, section_texts: dict, integrity_per_section: dict,
                          sonnet_pairing: dict) -> dict:
    facts = q.get("required_facts", [])
    facts_numbered = "\n".join(f"[{i+1}] {f}" for i, f in enumerate(facts))
    bundle_parts = []
    for sid, text in section_texts.items():
        if len(text) > 25000:
            text = text[:25000] + "\n\n[truncated]"
        bundle_parts.append(f"### {sid}\n{text}")
    section_bundle = "\n\n".join(bundle_parts)[:120000]
    first_verdicts = sonnet_pairing.get("per_fact_verdicts", [])
    verdicts_str = "\n".join(
        f"[{v.get('idx','?')}] verdict={v.get('verdict','?')}: {v.get('rationale','')}"
        for v in first_verdicts
    )
    prompt = ADVERSARIAL_VALIDATION_PROMPT.format(
        question_text=q["question_text"],
        required_facts_numbered=facts_numbered,
        section_bundle=section_bundle,
        integrity_notice=format_integrity_notice(integrity_per_section),
        canonical_answer_summary=sonnet_pairing.get("canonical_answer_summary", "(not available)"),
        override_triggers=", ".join(sonnet_pairing.get("override_triggers_detected", [])) or "(none flagged)",
        first_auditor_verdicts=verdicts_str or "(none)",
    )
    raw = call_gpt54_via_codex(prompt)
    if raw.startswith("[ERROR"):
        return {"_error": raw}
    return parse_json_safe(raw)


# ----------- Orchestration -----------

def audit_question(q: dict, integrity_cache: dict, client: LLMClient,
                   sonnet_model: str, skip_gpt54: bool = False) -> dict:
    # Collect unique sections cited in chain.path + source_section
    secs = set()
    if q.get("source_section"): secs.add(q["source_section"])
    for s in q.get("chain", {}).get("path", []): secs.add(s)
    section_texts = {}
    for sid in sorted(secs):
        t = load_section_md(sid)
        if t: section_texts[sid] = t

    # Module 1 (cached per section across questions)
    integrity_per_section = {}
    for sid in section_texts:
        if sid not in integrity_cache:
            integrity_cache[sid] = run_source_integrity(sid, client, sonnet_model)
        integrity_per_section[sid] = integrity_cache[sid]

    # Module 2
    sonnet_pairing = run_sonnet_pairing(q, section_texts, integrity_per_section, client, sonnet_model)

    # Module 3 (optional)
    gpt54_review = None
    if not skip_gpt54:
        gpt54_review = run_gpt54_adversarial(q, section_texts, integrity_per_section, sonnet_pairing)

    # L2 post-hoc quote verification: demote verdicts whose contradicting_source_quote is not
    # a substring of any cited section text. Protects against hallucinated quotes.
    corpus = "\n\n".join(section_texts.values())
    def _norm(s): return re.sub(r"\s+", " ", (s or "").strip())
    def _verify(entry, quote_key, verdict_key, verified_val="verified"):
        q_ = _norm(entry.get(quote_key, ""))
        v_ = entry.get(verdict_key)
        if v_ not in (verified_val, "uncertain") and (not q_ or _norm(corpus).find(q_) < 0):
            entry[f"{quote_key}_verified"] = False
            entry[f"{verdict_key}_original"] = v_
            entry[verdict_key] = "uncertain"
        else:
            entry[f"{quote_key}_verified"] = bool(q_) and v_ not in (verified_val, "uncertain")

    for v in sonnet_pairing.get("per_fact_verdicts", []) or []:
        _verify(v, "contradicting_source_quote", "verdict")
    if gpt54_review:
        for v in gpt54_review.get("per_fact_review", []) or []:
            _verify(v, "contradicting_source_quote", "my_verdict")

    return {
        "qid": q["id"],
        "tier": q["tier"],
        "n_facts": len(q.get("required_facts", [])),
        "integrity_per_section": integrity_per_section,
        "sonnet_pairing": sonnet_pairing,
        "gpt54_review": gpt54_review,
    }


def compute_pipeline_flag(audit_result: dict) -> dict:
    """Aggregate per-fact pipeline verdict: flagged if either module finds an issue (not 'verified')."""
    n = audit_result["n_facts"]
    pipeline_flags = {}
    sonnet_verdicts = {v.get("idx"): v for v in audit_result.get("sonnet_pairing", {}).get("per_fact_verdicts", [])}
    gpt54_verdicts = {v.get("idx"): v for v in (audit_result.get("gpt54_review") or {}).get("per_fact_review", [])}

    for i in range(1, n + 1):
        sv = sonnet_verdicts.get(i, {})
        gv = gpt54_verdicts.get(i, {})
        s_verdict = sv.get("verdict", "unknown")
        g_verdict = gv.get("my_verdict", "unknown")
        s_flag = s_verdict not in ("verified", "unknown")
        g_flag = g_verdict not in ("verified", "unknown")
        pipeline_flags[i] = {
            "sonnet_flag": s_flag,
            "sonnet_verdict": s_verdict,
            "gpt54_flag": g_flag,
            "gpt54_verdict": g_verdict,
            "both_flag": s_flag and g_flag,
            "either_flag": s_flag or g_flag,
        }
    return pipeline_flags


def compare_to_sme3(audit_result: dict, sme3_verdicts_for_q: dict) -> dict:
    """Compute per-fact TP/FP/FN vs SME-3 ground truth.
    SME-3 flag = fail or uncertain. Pipeline flag = either_flag."""
    pipeline = compute_pipeline_flag(audit_result)
    tp = fp = fn = tn = 0
    per_fact = []
    for idx_0 in range(audit_result["n_facts"]):
        sme3_v = sme3_verdicts_for_q.get(str(idx_0), "pass")
        sme3_flagged = sme3_v in ("fail", "uncertain")
        pl = pipeline.get(idx_0 + 1, {})
        pipeline_flagged_either = pl.get("either_flag", False)
        pipeline_flagged_both = pl.get("both_flag", False)
        if sme3_flagged and pipeline_flagged_either: tp += 1
        elif not sme3_flagged and pipeline_flagged_either: fp += 1
        elif sme3_flagged and not pipeline_flagged_either: fn += 1
        else: tn += 1
        per_fact.append({
            "idx": idx_0 + 1, "sme3": sme3_v,
            "sonnet_verdict": pl.get("sonnet_verdict"),
            "gpt54_verdict": pl.get("gpt54_verdict"),
            "pipeline_either_flag": pipeline_flagged_either,
            "pipeline_both_flag": pipeline_flagged_both,
        })
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "per_fact": per_fact}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--validation-set", default="/workspace/regbench_pilot/auto_gt_audit_validation_set.json")
    p.add_argument("--sample-full", default="/workspace/regbench_pilot/sme_precision_sample_100.json")
    p.add_argument("--sme3", default="/workspace/regbench_pilot/regbench_precision_review_100_SME_3.json")
    p.add_argument("--out", default="/workspace/regbench_pilot/auto_gt_audit_results.json")
    p.add_argument("--model", default="claude-sonnet-4-6", help="Sonnet model for Modules 1+2")
    p.add_argument("--skip-gpt54", action="store_true", help="Skip Module 3 (for a dry run)")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    val = json.load(open(args.validation_set))
    full_sample = {q["id"]: q for q in json.load(open(args.sample_full))}
    sme3 = json.load(open(args.sme3))

    qlist = val["errored_20"] + val["clean_20"]
    if args.limit:
        qlist = qlist[:args.limit]

    log(f"Auditing {len(qlist)} questions (20 errored + 20 clean, limit={args.limit})")
    log(f"Module 1+2 model: {args.model}; Module 3: {'SKIPPED' if args.skip_gpt54 else 'gpt-5.4 via codex CLI'}")

    client = LLMClient(
        cache_dir="/workspace/regbench_pilot/auto_gt_audit_cache",
        token_log_path="/workspace/regbench_pilot/auto_gt_audit_token_log.jsonl",
    )

    integrity_cache = {}
    results = []
    for i, meta in enumerate(qlist):
        qid = meta["qid"]
        q = full_sample.get(qid)
        if not q:
            log(f"[{i+1}/{len(qlist)}] {qid}: NOT IN SAMPLE, skipping")
            continue
        log(f"[{i+1}/{len(qlist)}] {qid} (T{q['tier']}, sme3={meta['sme3_verdict']})")
        r = audit_question(q, integrity_cache, client, args.model, skip_gpt54=args.skip_gpt54)
        r["sme3_meta"] = meta
        sme3_facts = sme3.get(qid, {}).get("facts", {})
        r["eval_vs_sme3"] = compare_to_sme3(r, sme3_facts)
        results.append(r)
        ev = r["eval_vs_sme3"]
        log(f"    TP={ev['tp']} FP={ev['fp']} FN={ev['fn']} TN={ev['tn']}")
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))

    # Aggregate
    tot_tp = sum(r["eval_vs_sme3"]["tp"] for r in results)
    tot_fp = sum(r["eval_vs_sme3"]["fp"] for r in results)
    tot_fn = sum(r["eval_vs_sme3"]["fn"] for r in results)
    tot_tn = sum(r["eval_vs_sme3"]["tn"] for r in results)
    recall = tot_tp / max(tot_tp + tot_fn, 1)
    precision = tot_tp / max(tot_tp + tot_fp, 1)
    log(f"\n{'='*50}\nOverall (facts pooled across {len(results)} Qs)\n{'='*50}")
    log(f"TP={tot_tp}  FP={tot_fp}  FN={tot_fn}  TN={tot_tn}")
    log(f"Recall:    {100*recall:.1f}%")
    log(f"Precision: {100*precision:.1f}%")
    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    log(f"\nSaved → {args.out}")


if __name__ == "__main__":
    main()
