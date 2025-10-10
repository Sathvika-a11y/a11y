#!/usr/bin/env python3
# core/rag_reviewer.py
import json, os, pathlib, re
from typing import Dict, Any, List, Tuple
from hashlib import sha256

BASE = pathlib.Path(__file__).parent.parent.resolve()

# ===================== helpers =====================

_SC_REGEX = re.compile(r"(\d)\.(\d)\.(\d)")
_WCAG_TAG_REGEX = re.compile(r"wcag(\d)(\d)(\d)$", re.IGNORECASE)

def _norm_sc_from_topic(topic: str) -> str:
    """Accept 'SC-1.1.1', '1.1.1', 'wcag111' → '1.1.1' (or '' if none)."""
    t = (topic or "").strip()
    m = _SC_REGEX.search(t)
    if m:
        return ".".join(m.groups())
    m = _WCAG_TAG_REGEX.search(t)
    if m:
        return ".".join(m.groups())
    t = t.lower().replace("sc-", "").strip()
    m = _SC_REGEX.search(t)
    return ".".join(m.groups()) if m else ""

def _scs_from_candidate(c: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    Extract primary SC and all SCs from candidate.
    Prefers candidate['sc_list'] (from axe tags). Falls back to topic.
    """
    sc_list: List[str] = []
    for t in (c.get("sc_list") or []):
        m = _WCAG_TAG_REGEX.match(str(t))
        if m:
            sc_list.append(".".join(m.groups()))
    sc_primary = sc_list[0] if sc_list else (_norm_sc_from_topic(c.get("topic", "")) or "")
    return sc_primary, sc_list

def load_techniques() -> List[Dict[str, Any]]:
    lib = []
    for p in (BASE / "wcag_lib").glob("*.json"):
        try:
            lib.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return lib

def retrieve_for_sc(sc: str, techniques: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Find a techniques doc for the SC (e.g., '1.3.1'). Fallback to {}."""
    sc_l = sc.lower()
    for t in techniques:
        tags = {str(x).lower() for x in t.get("tags", [])}
        topic = (t.get("topic") or "").lower()
        if f"sc-{sc_l}" in tags or sc_l in tags or sc_l in topic:
            return t
    return {}

def _synth_context(sc: str, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal context if wcag_lib lacks a doc."""
    out = {
        "topic": f"SC {sc}" if sc else "Unmapped rule",
        "do": [
            "Apply WCAG techniques conservatively for this SC.",
            "Prefer 'review' when meaning, purpose, or evidence is unclear."
        ],
        "dont": [
            "Do not approve ambiguous or redundant alternatives without evidence.",
            "Do not rely on visual presentation alone."
        ],
        "edge_cases": []
    }
    if candidate.get("axe_help"):
        out["do"].append(f"Consider axe help: {candidate.get('axe_help')}")
    if candidate.get("axe_help_url"):
        out["do"].append(f"Ref: {candidate.get('axe_help_url')}")
    return out

# Normalize any legacy verdicts to pass|fail|review
def _normalize_verdict(v: str) -> str:
    v = (v or "").strip().lower()
    mapping = {
        "needs-change": "fail",
        "decorative-ok": "pass",
        "redundant-ok": "pass",
        "complex-needs-longdesc": "review",
    }
    return mapping.get(v, v if v in {"pass","fail","review"} else "review")

# ===================== prompt builder =====================

def build_prompt(template_path: pathlib.Path, sc: str, tech_doc: Dict[str, Any], c: Dict[str, Any]) -> str:
    """
    Build the semantic review prompt for one candidate.
    Appends AXE_DIAGNOSTICS after formatting. Raises a clear error if the template has unescaped braces.
    """
    # If no technique doc, synthesize a tiny one and fold in axe help as guidance
    if not tech_doc:
        tech_doc = _synth_context(sc, c)

    techniques_context = json.dumps({
        "topic": tech_doc.get("topic"),
        "do": tech_doc.get("do"),
        "dont": tech_doc.get("dont"),
        "edge_cases": tech_doc.get("edge_cases"),
    }, indent=2, ensure_ascii=False)

    tpl = template_path.read_text(encoding="utf-8")

    # Include axe WHY fields to improve semantic judgment
    why_pack = {
        "failureSummary": c.get("failureSummary"),
        "why_any": c.get("why_any"),
        "why_all": c.get("why_all"),
        "why_none": c.get("why_none"),
        "page_url": c.get("page_url"),
    }

    # Variables to interpolate
    fmt_vars = {
        "topic_label": (f"SC {sc}" if sc else (c.get("topic") or "Unmapped")),
        "techniques_context": techniques_context,
        "selector": c.get("selector", ""),
        "html_snippet": (c.get("html_snippet", "")[:1200] or ""),
        "attributes": json.dumps(c.get("attributes", {}), ensure_ascii=False),
        "role_name": c.get("role_name_guess", ""),
        "nearby_text": (c.get("nearby_text", "")[:800] or ""),
        "acc_snapshot": json.dumps(c.get("acc_snapshot", {}), ensure_ascii=False)[:1200],
        "rule_id": c.get("axe_rule_id", ""),
        "axe_help": c.get("axe_help", ""),
        "impact": c.get("impact", ""),
    }

    try:
        prompt = tpl.format(**fmt_vars)
    except KeyError as e:
        # Most common cause: literal { } in the template JSON block not escaped as {{ }}
        raise RuntimeError(
            "Template formatting error near placeholder "
            f"{e!s}. You likely have an unescaped '{{' or '}}' in the template. "
            "Double all literal braces in JSON examples (use '{{' and '}}')."
        ) from e
    except ValueError as e:
        # Unmatched '{' or '}' or invalid format string
        raise RuntimeError(
            "Template formatting error (invalid format string). "
            "Ensure all literal braces are escaped as '{{' and '}}'."
        ) from e

    # Append diagnostics after successful formatting
    prompt += "\n\nAXE_DIAGNOSTICS:\n" + json.dumps(why_pack, ensure_ascii=False, indent=2)
    return prompt

# ===================== LLM switch =====================

def _use_live_llm() -> bool:
    return os.environ.get("A11Y_USE_LLM") == "1" and bool(os.environ.get("OPENAI_API_KEY"))

def _openai_chat_json(messages: List[Dict[str,str]], model: str) -> str:
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type":"json_object"},
        messages=messages
    )
    return resp.choices[0].message.content

def _run_llm_openai(prompt: str) -> Dict[str, Any]:
    """
    OpenAI call using Chat Completions → *pass/fail/review only*.
    """
    try:
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        text = _openai_chat_json(
            [
                {
                    "role":"system",
                    "content":(
                        "You are an accessibility reviewer. "
                        "Return ONLY a compact JSON object with keys: "
                        "type, verdict, reason, confidence, techniques_used. "
                        "The 'verdict' must be exactly one of: pass, fail, review."
                    )
                },
                {"role":"user","content":prompt}
            ],
            model=model
        )
        s = text.strip()
        s = s[s.find("{") : s.rfind("}")+1]
        data = json.loads(s)
        # Normalize verdict to our 3-state vocabulary
        data["verdict"] = _normalize_verdict(data.get("verdict"))
        for k in ["type","verdict","reason","confidence","techniques_used"]:
            if k not in data:
                raise ValueError(f"Missing key: {k}")
        return data
    except Exception as e:
        # Soft fallback → review (not a hard fail)
        return {
            "type": "informative",
            "verdict": "review",
            "reason": f"AI error/parse fallback: {e}",
            "confidence": 0.3,
            "techniques_used": ["fallback"]
        }

def run_llm(prompt: str) -> Dict[str, Any]:
    if _use_live_llm():
        return _run_llm_openai(prompt)
    # Offline-safe stub → review
    return {
        "type": "informative",
        "verdict": "review",
        "reason": "Demo verdict (no live LLM or A11Y_USE_LLM=0).",
        "confidence": 0.4,
        "techniques_used": ["demo-only"]
    }

# ======== 2.4.6 AI batch (guaranteed output) ========
def _ai_judge_246(items: List[Dict[str,Any]], url: str) -> List[Dict[str,Any]]:
    """
    Produce ai/2_4_6/results.json style rows (pass|fail|review).
    Uses OPENAI_API_KEY when A11Y_USE_LLM=1. Model from A11Y_246_MODEL or OPENAI_MODEL (fallback gpt-4o-mini).
    If live AI is off OR returns nothing, synthesize results from heuristics so the reports still populate.
    """
    out: List[Dict[str,Any]] = []
    if not items:
        return out

    # Heuristic fallback (used if live off, or if live returns nothing)
    def _fallback(rows: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
        res: List[Dict[str,Any]] = []
        for it in rows:
            typ = it.get("type") or "heading"
            sel = it.get("selector") or ""
            txt = (it.get("visibleText") or it.get("visibleLabel") or it.get("accessibleName") or "").strip()
            if not txt or len(txt) <= 2:
                verdict, reasons = "fail", ["Very short/empty text"]
            else:
                verdict, reasons = "review", []
            res.append({
                "selector": sel,
                "type": typ,
                "sc": "2.4.6",
                "verdict": verdict,
                "reasons": reasons,
                "suggestion": ""
            })
        return res

    if not _use_live_llm():
        return _fallback(items)

    # live AI: chunked prompts
    model = os.environ.get("A11Y_246_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    sys_msg = (
        "You are an accessibility auditor focused strictly on WCAG 2.4.6 (Headings and Labels). "
        "Headings must describe the topic/purpose. Labels (buttons/links/inputs) must indicate purpose/action. "
        "Use nearbyText/region when helpful. Return STRICT JSON with a top-level key 'results' whose value is an array of objects: "
        "{selector,type,sc:'2.4.6',verdict in ['pass','fail','review'],reasons[],suggestion}."
    )

    CHUNK = 35
    for i in range(0, len(items), CHUNK):
        batch = items[i:i+CHUNK]
        user_payload = {
            "url": url,
            "items": batch,
            "output_format": [
                {"selector":"...", "type":"heading|label", "sc":"2.4.6",
                 "verdict":"pass|fail|review", "reasons":["..."], "suggestion":"..."}
            ]
        }
        try:
            content = _openai_chat_json(
                [
                    {"role":"system","content": sys_msg},
                    {"role":"user","content": json.dumps(user_payload, ensure_ascii=False)}
                ],
                model=model
            )
            data = json.loads(content)
            rows = data.get("results") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                rows = []
        except Exception as e:
            # Log the error and continue (we'll fallback later if needed)
            try:
                errp = BASE / "ai_2_4_6_errors.log"
                with errp.open("a", encoding="utf-8") as ef:
                    ef.write(f"[chunk {i//CHUNK}] {type(e).__name__}: {e}\n")
            except Exception:
                pass
            rows = []

        for r in rows:
            r["sc"] = "2.4.6"
            r["type"] = r.get("type") or "heading"
            r["selector"] = r.get("selector") or ""
            r["verdict"] = _normalize_verdict(r.get("verdict"))
            r["reasons"] = r.get("reasons") or []
            if r["verdict"] in ("pass","fail","review"):
                out.append(r)

    # If AI yielded nothing, guarantee output with fallback
    if not out:
        out = _fallback(items)
    return out

# ===================== main review =====================

def review(out_dir: pathlib.Path):
    out_dir = pathlib.Path(out_dir)
    candidates_path = out_dir / "candidates.json"
    if not candidates_path.exists():
        raise FileNotFoundError("candidates.json not found; run axe_runner first.")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    techniques = load_techniques()
    debug_flags = {
    "A11Y_USE_LLM": os.environ.get("A11Y_USE_LLM"),
    "OPENAI_API_KEY_present": bool(os.environ.get("OPENAI_API_KEY")),
    "OPENAI_MODEL": os.environ.get("OPENAI_MODEL"),
    "A11Y_246_MODEL": os.environ.get("A11Y_246_MODEL"),
    }
    (out_dir / "ai_env_debug.json").write_text(json.dumps(debug_flags, indent=2), encoding="utf-8")

    prompts_dir = out_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    skip_best_practice = os.environ.get("A11Y_SKIP_BEST_PRACTICE") == "1"

    results = []
    for i, c in enumerate(candidates):
        # Determine SCs
        sc_primary, sc_all = _scs_from_candidate(c)

        # Optionally skip non-WCAG candidates
        is_wcag = bool(sc_primary)
        if skip_best_practice and not is_wcag:
            continue

        # Retrieve technique doc for primary SC (if any)
        tech_doc = retrieve_for_sc(sc_primary, techniques) if is_wcag else {}

        # Build prompt
        template_path = BASE / "prompts" / "semantic_review_template.txt"
        prompt = build_prompt(template_path, sc_primary, tech_doc, c)

        # Persist prompt for audit
        pfile = prompts_dir / f"{i:03d}_{sc_primary or (c.get('topic') or 'UNMAPPED')}_{c.get('axe_rule_id','')}.txt"
        pfile.write_text(prompt, encoding="utf-8")

        # Run AI (or stub) and normalize verdict
        verdict = run_llm(prompt)
        verdict["verdict"] = _normalize_verdict(verdict.get("verdict"))

        # Traceability hash
        prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()[:16]

        results.append({
            "page_url": c.get("page_url"),
            "topic": c.get("topic"),
            "SC": sc_primary,            # normalized SC (e.g., '1.1.1'), '' if non-WCAG
            "sc_list": sc_all,           # all SCs from axe tags
            "selector": c.get("selector"),
            "axe_rule_id": c.get("axe_rule_id"),
            "impact": c.get("impact"),
            "ai_verdict": verdict,
            "screenshot": c.get("screenshot"),
            "axe_help_url": c.get("axe_help_url"),
            "prompt_hash": prompt_hash
        })

    (out_dir / "ai_verdicts.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    # --------- 2.4.6 AI pass (reads input from axe runner, writes results.json) ----------
    try:
        ai_dir = out_dir / "ai" / "2_4_6"
        ai_dir.mkdir(parents=True, exist_ok=True)
        inp = ai_dir / "input.json"
        if inp.exists():
            items = json.loads(inp.read_text(encoding="utf-8"))
            # Try to get URL for context; ok if missing
            url = ""
            try:
                meta_p = out_dir / "metadata.json"
                if meta_p.exists():
                    meta = json.loads(meta_p.read_text(encoding="utf-8"))
                    url = meta.get("page_url", "") or ""
            except Exception:
                url = ""
            ai246 = _ai_judge_246(items, url)
            (ai_dir / "results.json").write_text(json.dumps(ai246, indent=2), encoding="utf-8")
    except Exception as e:
        # Do not fail the whole review if 2.4.6 pass has an issue
        try:
            with (out_dir / "ai" / "2_4_6" / "errors.log").open("a", encoding="utf-8") as ef:
                ef.write(f"{type(e).__name__}: {e}\n")
        except Exception:
            pass

    return {"reviewed": len(results)}

# --------------------- CLI ---------------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", help="Directory containing candidates.json (output of axe_runner).")
    args = ap.parse_args()
    res = review(pathlib.Path(args.out_dir))
    print(json.dumps(res))
