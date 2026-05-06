#!/usr/bin/env python3
"""
Run codex CLI (GPT-5.4) as the answerer baseline on v7 pilot questions.

Uses the SAME ANSWER_PROMPT + oracle context collection as run_text_baseline.py,
so results are directly comparable to Sonnet/Opus/Haiku oracle runs.

Outputs same JSON format; judged separately by judge_rag_results.py or inline.
"""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path
from datetime import datetime

# os.environ.setdefault("all_proxy", "socks5://127.0.0.1:1084")  # uncomment to route via local SOCKS bridge
sys.path.insert(0, str(Path(__file__).parent))

# Reuse the exact same prompt + context collection as run_text_baseline
from run_text_baseline import (
    ANSWER_PROMPT, collect_oracle_context, collect_full_context,
    JUDGE_PROMPT, strip_to_json
)
from llm_client import LLMClient


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# Tool-invocation markers codex emits in its session log when it actually uses tools.
# These are distinct from normal text content.
TOOL_INVOCATION_MARKERS = (
    "exec_shell", "apply_patch", "function_call", "tool_use",
    "shell_command", "bash_command",
    # codex session log markers when it opens shell commands
    "\nbash ", "\ncat /workspace", "\ngrep ", "\nfind /workspace",
    "\nls /workspace", "\nls -", "\nhead ", "\ntail ",
)

# Content markers that indicate the response references the hidden ground truth.
GROUND_TRUTH_MARKERS = (
    "pilot_49_v7_with_gold_pages", "regbench_v7_annotations",
    "required_facts", "annotator_grounding", "expected_derivation",
    "chain_summary",
)


def _scan_for_leak_markers(stdout: str, stderr: str, response: str) -> dict:
    """Detect actual tool invocations (from codex session log) or ground-truth
    references in the final response. We deliberately do NOT scan the echoed
    prompt — that contains our own anti-leak preamble listing forbidden paths."""
    tool_hits = []
    gt_hits = []

    # Tool invocations appear in codex's session log on stdout, not the final message.
    session_log = stdout or ""
    for m in TOOL_INVOCATION_MARKERS:
        if m.lower() in session_log.lower():
            # Skip matches that are just echoing our own prompt's forbidden-paths list
            if "anti-leak" in session_log.lower() or "STRICT RULES FOR THIS TASK" in session_log:
                # Look for the marker OUTSIDE the anti-leak preamble region
                idx = session_log.lower().find(m.lower())
                preamble_end = session_log.find("---\n\nYou are a ship-design")
                if preamble_end > 0 and idx < preamble_end:
                    continue
            tool_hits.append(m.strip())

    # Ground-truth field names appearing in the actual response (not prompt)
    for m in GROUND_TRUTH_MARKERS:
        if m.lower() in (response or "").lower():
            gt_hits.append(m)

    return {
        "tool_invocations": tool_hits,
        "ground_truth_refs": gt_hits,
        "clean": len(tool_hits) == 0 and len(gt_hits) == 0,
    }


def call_codex(prompt: str, model: str = "gpt-5.4", timeout: int = 600) -> tuple[str, dict]:
    """Invoke codex exec, return (last_message, audit_record)."""
    out_file = Path("/tmp/codex_out.txt")
    out_file.write_text("")  # clear

    # Isolation:
    # - working dir = /root/codex_sandbox (non-tmp so codex accepts it as HOME)
    # - cleared of files between questions
    # - --ephemeral: no session persistence
    # - --sandbox read-only: can't write
    # - stripped env to minimal paths
    # Full root-namespace isolation (bwrap/unshare) is unavailable in this container.
    sandbox_dir = Path("/root/codex_sandbox")
    sandbox_dir.mkdir(exist_ok=True)
    for f in sandbox_dir.iterdir():
        try:
            if f.is_file(): f.unlink()
        except: pass

    # Keep real HOME so codex finds its OAuth credentials, but restrict cwd.
    # The leak defense comes from: empty cwd, read-only sandbox, --ephemeral,
    # anti-leak prompt preamble, and audit logging.
    minimal_env = dict(os.environ)  # inherit base env
    minimal_env["PATH"] = "/usr/local/share/npm-global/bin:/usr/local/bin:/usr/bin:/bin"
    # Preserve codex auth
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_AUTH_TOKEN", "all_proxy"):
        if k in os.environ:
            minimal_env[k] = os.environ[k]

    try:
        proc = subprocess.run(
            ["codex", "exec", "-m", model,
             "--ephemeral",
             "-C", str(sandbox_dir),
             "--skip-git-repo-check",
             "--sandbox", "read-only",
             "-o", str(out_file)],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=minimal_env,
            cwd=str(sandbox_dir),
        )
        # Prefer the captured output file (the actual last assistant message).
        # Fall back to stdout only if the file is empty (edge case).
        out = out_file.read_text().strip()
        if not out and proc.returncode != 0:
            return (f"[ERROR: codex returncode={proc.returncode} stderr={proc.stderr[:500]}]",
                    {"tool_invocations": [], "ground_truth_refs": [], "clean": False,
                     "error": proc.stderr[:500]})
        audit = _scan_for_leak_markers(proc.stdout, proc.stderr, out)
        return out, audit
    except subprocess.TimeoutExpired:
        return "[ERROR: codex timeout]", {"tool_invocations": [], "ground_truth_refs": [],
                                          "clean": False, "error": "timeout"}
    except Exception as e:
        return f"[ERROR: {type(e).__name__}: {e}]", {"tool_invocations": [], "ground_truth_refs": [],
                                                     "clean": False, "error": str(e)}


def judge_response(q: dict, response: str, client: LLMClient, judge_model: str, cache_key: str) -> dict:
    required = q.get("required_facts") or []
    if not required:
        return {"is_correct": False, "error": "no_required_facts"}
    grounding = q.get("annotator_grounding", {}) or {}
    expected = grounding.get("expected_derivation") or grounding.get("chain_summary") or ""
    req_str = "\n".join(f"- {f}" for f in required)
    prompt = JUDGE_PROMPT.format(
        question=q.get("question_text", ""),
        expected=expected,
        required_facts=req_str,
        response=response,
    )
    raw = client.call_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=judge_model, max_tokens=16384, temperature=0.0,
        cache_key=cache_key,
    )
    raw = strip_to_json(raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {"is_correct": False, "error": "judge_parse_fail", "raw": raw[:500]}
    try:
        return json.loads(raw[i:j+1])
    except json.JSONDecodeError:
        return {"is_correct": False, "error": "judge_json_fail", "raw": raw[:500]}


_BM25_CACHE = {"bm25": None, "sections": None, "texts": None}


def _get_bm25():
    if _BM25_CACHE["bm25"] is None:
        from run_bm25_baseline import discover_all_sections, build_bm25_index
        secs = discover_all_sections()
        bm25, vs, texts = build_bm25_index(secs)
        _BM25_CACHE["bm25"] = bm25
        _BM25_CACHE["sections"] = vs
        _BM25_CACHE["texts"] = texts
    return _BM25_CACHE["bm25"], _BM25_CACHE["sections"], _BM25_CACHE["texts"]


def evaluate_question(q: dict, condition: str, client: LLMClient, codex_model: str,
                      judge_model: str, use_raw: bool) -> dict:
    if condition == "oracle":
        context, used = collect_oracle_context(q, use_raw=use_raw)
        ctx_stats = {"sections_used": used}
    elif condition == "full_context":
        context, used = collect_full_context(q, use_raw=use_raw)
        ctx_stats = {"sections_used": used}
    elif condition == "bm25":
        from run_bm25_baseline import retrieve_topk
        bm25, sections, texts = _get_bm25()
        results = retrieve_topk(bm25, sections, texts, q["question_text"], k=10)
        context_parts = [f"## {sid}\n\n{text}" for sid, text, _ in results]
        context = "\n\n".join(context_parts)
        if len(context) > 600_000:
            context = context[:600_000] + "\n[truncated]"
        ctx_stats = {"sections_used": [r[0] for r in results]}
        used = ctx_stats["sections_used"]
    elif condition == "chainrag":
        from run_chainrag_baseline import build_chainrag_context
        bm25, sections, texts = _get_bm25()
        context, stats = build_chainrag_context(
            bm25, sections, texts, q["question_text"],
            top_k=5, max_hop1=10, max_chars=400_000
        )
        ctx_stats = stats
        used = stats["sections_used"]
    else:
        raise ValueError(f"Unknown condition: {condition}")

    prompt = ANSWER_PROMPT.format(context=context, question=q.get("question_text", ""))
    # Anti-leak preamble: forbid filesystem/shell access to benchmark files.
    # The codex CLI has workspace-write access and could trivially cat the
    # ground-truth JSON (required_facts, expected_derivation, annotator_grounding)
    # if not explicitly blocked. This prefix ensures fair oracle evaluation.
    anti_leak = """\
STRICT RULES FOR THIS TASK (violating these invalidates your response):
1. DO NOT run any shell commands, read any files, or use any tools.
2. DO NOT access /workspace/regbench_pilot/, /workspace/regbench_500/, or any
   file containing 'pilot_', 'regbench', 'annotations', 'required_facts',
   'annotator_grounding', or 'expected_derivation'.
3. DO NOT search the web, use grep, cat, find, ls, or any filesystem operation.
4. Answer ONLY from the SOURCE DOCUMENTS provided below and your own reasoning.
5. Treat this as a closed evaluation: the only inputs are the prompt text below.

Any response that relies on information outside the prompt below will be
rejected as a benchmark leak.

---

"""
    full_prompt = anti_leak + prompt
    response, audit = call_codex(full_prompt, model=codex_model)

    import hashlib
    resp_hash = hashlib.sha256(response.encode()).hexdigest()[:8]
    verdict = judge_response(
        q, response, client, judge_model,
        cache_key=f"judge_codex_v2_{codex_model}_{condition}_{q['id']}_{resp_hash}_{judge_model}".replace(".", "_")
    )

    return {
        "id": q["id"],
        "tier": q["tier"],
        "condition": condition,
        "sections_used": used,
        "context_chars": len(context),
        "response": response,
        "verdict": verdict,
        "is_correct": bool(verdict.get("is_correct", False)),
        "audit": audit,
        **{k: v for k, v in (ctx_stats or {}).items() if k != "sections_used"},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", default="/workspace/regbench_pilot/pilot_49_v7_with_gold_pages.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="gpt-5.4", help="codex CLI model")
    parser.add_argument("--judge", default="claude-sonnet-4-6")
    parser.add_argument("--conditions", default="full_context",
                        help="Comma-separated: 'full_context' (primary — realistic, distractor-heavy) or 'oracle' (ceiling reference)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    questions = json.loads(Path(args.input).read_text())
    if args.limit:
        questions = questions[:args.limit]
    log(f"Loaded {len(questions)} questions from {args.input}")
    log(f"Codex answer model: {args.model}, judge: {args.judge}")

    client = LLMClient(
        cache_dir="/workspace/regbench_pilot/codex_cache",
        token_log_path="/workspace/regbench_pilot/codex_token_log.jsonl",
    )

    # Resume mode: load existing results, skip successful ones, retry errors
    results = []
    existing = {}
    if Path(args.out).exists():
        try:
            prev = json.loads(Path(args.out).read_text())
            for r in prev:
                # Keep only successful runs; we'll retry errors
                if r.get('response','').startswith('[ERROR'):
                    continue
                existing[(r['id'], r['condition'])] = r
            log(f"Resume mode: found {len(existing)} prior successful results to keep")
        except Exception as e:
            log(f"Could not load prior results: {e}")

    for cond in args.conditions.split(","):
        log(f"\n=== CONDITION: {cond} ===")
        for i, q in enumerate(questions):
            key = (q['id'], cond)
            if key in existing:
                results.append(existing[key])
                r = existing[key]
                mark = "✓" if r["is_correct"] else "✗"
                fp = r["verdict"].get("facts_present", "?")
                ft = r["verdict"].get("facts_total", "?")
                log(f"  [{i+1}/{len(questions)}] (cached) {mark} T{q['tier']} {q['id']} "
                    f"facts={fp}/{ft}")
                Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
                continue
            r = evaluate_question(q, cond, client, args.model, args.judge, args.raw)
            results.append(r)
            mark = "✓" if r["is_correct"] else "✗"
            fp = r["verdict"].get("facts_present", "?")
            ft = r["verdict"].get("facts_total", "?")
            log(f"  [{i+1}/{len(questions)}] {mark} T{q['tier']} {q['id']} "
                f"{r['context_chars']:,} chars  facts={fp}/{ft}")
            # Save incrementally
            Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))

    # Aggregate
    log(f"\n{'='*60}")
    log(f"RESULTS — codex {args.model} judged by {args.judge}")
    log(f"{'='*60}")
    for cond in args.conditions.split(","):
        log(f"\n--- {cond} ---")
        tiers = sorted(set(r["tier"] for r in results))
        tot_c = tot_n = 0
        for tier in tiers:
            cr = [r for r in results if r["condition"] == cond and r["tier"] == tier]
            if not cr: continue
            c = sum(1 for r in cr if r["is_correct"])
            n = len(cr)
            log(f"T{tier}: {c}/{n} ({100*c/n:.1f}%)")
            tot_c += c; tot_n += n
        log(f"TOTAL: {tot_c}/{tot_n} ({100*tot_c/max(tot_n,1):.1f}%)")

    # Audit summary
    clean = sum(1 for r in results if r.get("audit", {}).get("clean"))
    flagged = [r for r in results if not r.get("audit", {}).get("clean")]
    log(f"\n=== AUDIT ===")
    log(f"Clean runs (no tool invocations, no ground-truth refs): {clean}/{len(results)}")
    if flagged:
        log(f"Flagged ({len(flagged)}):")
        for r in flagged[:10]:
            a = r.get('audit', {}) or {}
            log(f"  {r['id']}: tool={a.get('tool_invocations')} gt={a.get('ground_truth_refs')} err={a.get('error','')[:80]}")


if __name__ == "__main__":
    main()
