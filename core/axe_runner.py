#!/usr/bin/env python3
import json, os, time, re, pathlib, argparse
from typing import Any, Dict, Optional, List
from PIL import Image
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

AXE_URLS = [
    "https://cdn.jsdelivr.net/npm/axe-core@4.7.2/axe.min.js",
    "https://unpkg.com/axe-core@4.7.2/axe.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.7.2/axe.min.js",
]

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def sanitize_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s)
    return s[:120]

def _extract_scs(tags: List[str]) -> List[str]:
    scs = []
    for t in tags or []:
        m = re.match(r"wcag(\d)(\d)(\d)$", str(t).lower())
        if m:
            scs.append(".".join(m.groups()))
    return scs

def _primary_sc(scs: List[str]) -> Optional[str]:
    return scs[0] if scs else None

def _msgs(items: List[Dict[str, Any]]) -> List[str]:
    out = []
    for it in items or []:
        mid = it.get("id") or ""
        msg = it.get("message") or it.get("data") or ""
        out.append(f"{mid}: {msg}".strip(": ").strip())
    return out

def crop_element_screenshot(page, selector: str, out_path: pathlib.Path) -> Optional[str]:
    try:
        el = page.query_selector(selector)
        if not el:
            return None
        box = el.bounding_box()
        if not box:
            return None
        clip = {
            "x": max(0, box["x"] - 2),
            "y": max(0, box["y"] - 2),
            "width": box["width"] + 4,
            "height": box["height"] + 4,
        }
        tmp = out_path.parent / (out_path.stem + "_full.png")
        page.screenshot(path=str(tmp), full_page=True)
        im = Image.open(tmp)
        x = int(clip["x"]); y = int(clip["y"])
        w = int(clip["width"]); h = int(clip["height"])
        w = min(w, im.width - x)
        h = min(h, im.height - y)
        if w <= 0 or h <= 0:
            return None
        crop = im.crop((x, y, x+w, y+h))
        crop.save(out_path)
        try: tmp.unlink()
        except Exception: pass
        return str(out_path)
    except Exception:
        return None

def get_accessibility_snapshot(page, selector: str) -> Dict[str, Any]:
    try:
        el = page.query_selector(selector)
        if not el:
            return {}
        snap = page.accessibility.snapshot(root=el)
        def trim(node):
            if not isinstance(node, dict):
                return node
            keys = ["role", "name", "value", "description"]
            out = {k: node.get(k) for k in keys if k in node}
            if "children" in node:
                out["children"] = [trim(c) for c in node["children"][:4]]
            return out
        return trim(snap) or {}
    except Exception:
        return {}

def get_nearby_text(page, selector: str) -> str:
    js = """(sel) => {
      const el = document.querySelector(sel);
      if (!el) return "";
      const parent = el.closest('figure, a, button, label, td, th, p, div, section, article') || el.parentElement || document.body;
      const text = parent.innerText || "";
      return text.trim().slice(0, 500);
    }"""
    try:
        return page.evaluate(js, selector) or ""
    except Exception:
        return ""

def get_role_name_guess(page, selector: str) -> str:
    js = """(sel) => {
      const el = document.querySelector(sel);
      if (!el) return "";
      const role = el.getAttribute('role') || el.tagName.toLowerCase();
      const name = el.getAttribute('aria-label') || el.getAttribute('alt') || el.getAttribute('aria-labelledby') || el.innerText || "";
      return (role + " — " + name.trim()).slice(0, 200);
    }"""
    try:
        return page.evaluate(js, selector) or ""
    except Exception:
        return ""

def run_axe_on_url(url: str, out_dir: pathlib.Path, timeout_ms: int = 30000):
    ensure_dir(out_dir)
    screenshots_dir = out_dir / "screenshots"
    ensure_dir(screenshots_dir)

    # metadata with page URL (used in reports)
    (out_dir / "metadata.json").write_text(json.dumps({"page_url": url}, indent=2), encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
        except PlaywrightTimeoutError:
            page.goto(url, wait_until="load")
        time.sleep(0.6)

        # inject axe
        injected = False
        for u in AXE_URLS:
            try:
                page.add_script_tag(url=u)
                injected = True
                break
            except Exception:
                continue
        if not injected:
            raise RuntimeError("Failed to inject axe-core from CDN.")

        # run axe
        axe_payload = page.evaluate("""() => axe.run(document, { resultTypes: ['violations','incomplete','passes'] })""")
        (out_dir / "axe_results.json").write_text(json.dumps(axe_payload, indent=2), encoding="utf-8")

        # --------- write a node-by-node debug log (why it passed/failed) ----------
        nodes_log = out_dir / "axe_nodes.jsonl"
        with nodes_log.open("w", encoding="utf-8") as f:
            for bucket in ["violations","incomplete","passes"]:
                for r in axe_payload.get(bucket, []):
                    scs = _extract_scs(r.get("tags", []))
                    for n in r.get("nodes", []):
                        rec = {
                            "page_url": url,
                            "bucket": bucket,
                            "rule_id": r.get("id"),
                            "help": r.get("help"),
                            "helpUrl": r.get("helpUrl"),
                            "impact": r.get("impact"),
                            "sc_list": scs,
                            "selector": (n.get("target") or [""])[0],
                            "html": n.get("html"),
                            "failureSummary": n.get("failureSummary"),
                            "why_any": _msgs(n.get("any")),
                            "why_all": _msgs(n.get("all")),
                            "why_none": _msgs(n.get("none")),
                        }
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # --------- build candidates for AI reviewer ----------
        candidates = []
        seen = set()

        # must-review: everything axe marks as violations or incomplete → AI
        for bucket in ["violations", "incomplete"]:
            for r in axe_payload.get(bucket, []):
                scs = _extract_scs(r.get("tags", []))
                topic = f"SC-{_primary_sc(scs)}" if scs else "BEST_PRACTICE"
                for n in r.get("nodes", []):
                    selector = (n.get("target") or [""])[0]
                    if not selector:
                        continue
                    key = (r.get("id"), selector)
                    if key in seen:
                        continue
                    seen.add(key)
                    html_snippet = n.get("html") or ""
                    acc = get_accessibility_snapshot(page, selector)
                    nearby = get_nearby_text(page, selector)
                    role_name = get_role_name_guess(page, selector)
                    attrs = page.eval_on_selector(selector, "(el)=>{const o={};for (const a of el.getAttributeNames()) o[a]=el.getAttribute(a); return o;}") if page.query_selector(selector) else {}
                    shot_name = sanitize_filename(f"{r.get('id')}__{selector[:60]}") + ".png"
                    shot_path = (out_dir / "screenshots" / shot_name)
                    shot_saved = crop_element_screenshot(page, selector, shot_path)

                    candidates.append({
                        "page_url": url,
                        "bucket": "must_review",
                        "topic": topic,                       # SC-x.x.x or BEST_PRACTICE
                        "sc_list": scs,                       # all mapped SCs
                        "axe_rule_id": r.get("id"),
                        "axe_help": r.get("help"),
                        "axe_help_url": r.get("helpUrl"),
                        "impact": r.get("impact"),
                        "selector": selector,
                        "html_snippet": html_snippet,
                        "attributes": attrs,
                        "role_name_guess": role_name,
                        "nearby_text": nearby,
                        "acc_snapshot": acc,
                        "screenshot": shot_saved,
                        # why fields for reviewers (also in nodes log)
                        "failureSummary": n.get("failureSummary"),
                        "why_any": _msgs(n.get("any")),
                        "why_all": _msgs(n.get("all")),
                        "why_none": _msgs(n.get("none")),
                    })

        # passed-but-interesting: expand gently (kept small here)
        # image-alt → likely functional or generic alt → 1.1.1
        for r in axe_payload.get("passes", []):
            rid = r.get("id")
            if rid not in {"image-alt","link-name"}:
                continue
            scs = _extract_scs(r.get("tags", []))
            topic = f"SC-{_primary_sc(scs)}" if scs else "BEST_PRACTICE"

            if rid == "image-alt":
                for n in r.get("nodes", [])[:30]:
                    sel = (n.get("target") or [""])[0]
                    if not sel: continue
                    role_name = get_role_name_guess(page, sel).lower()
                    rn = role_name.split(" — ",1)[1] if " — " in role_name else role_name
                    generic = any(g in rn for g in ["image","photo","img_",".jpg",".png"])
                    is_functional = page.eval_on_selector(sel, "(el)=>!!el.closest('a,button')") if sel else False
                    if is_functional or generic:
                        key = (rid, sel)
                        if key in seen: continue
                        seen.add(key)
                        html_snippet = n.get("html") or ""
                        acc = get_accessibility_snapshot(page, sel)
                        nearby = get_nearby_text(page, sel)
                        attrs = page.eval_on_selector(sel, "(el)=>{const o={};for (const a of el.getAttributeNames()) o[a]=el.getAttribute(a); return o;}") if page.query_selector(sel) else {}
                        shot_name = sanitize_filename(f"{rid}__{sel[:60]}") + ".png"
                        shot_path = (out_dir / "screenshots" / shot_name)
                        shot_saved = crop_element_screenshot(page, sel, shot_path)
                        candidates.append({
                            "page_url": url,
                            "bucket": "semantic_review",
                            "topic": topic,
                            "sc_list": scs,
                            "axe_rule_id": rid,
                            "axe_help": r.get("help"),
                            "axe_help_url": r.get("helpUrl"),
                            "impact": r.get("impact"),
                            "selector": sel,
                            "html_snippet": html_snippet,
                            "attributes": attrs,
                            "role_name_guess": get_role_name_guess(page, sel),
                            "nearby_text": nearby,
                            "acc_snapshot": acc,
                            "screenshot": shot_saved,
                            "failureSummary": n.get("failureSummary"),
                            "why_any": _msgs(n.get("any")),
                            "why_all": _msgs(n.get("all")),
                            "why_none": _msgs(n.get("none")),
                        })
                        break

            if rid == "link-name":
                for n in r.get("nodes", [])[:30]:
                    sel = (n.get("target") or [""])[0]
                    if not sel: continue
                    txt = (page.inner_text(sel) or "").strip().lower() if page.query_selector(sel) else ""
                    if txt in {"click here","learn more","read more"}:
                        key = (rid, sel)
                        if key in seen: continue
                        seen.add(key)
                        html_snippet = n.get("html") or ""
                        acc = get_accessibility_snapshot(page, sel)
                        nearby = get_nearby_text(page, sel)
                        attrs = page.eval_on_selector(sel, "(el)=>{const o={};for (const a of el.getAttributeNames()) o[a]=el.getAttribute(a); return o;}") if page.query_selector(sel) else {}
                        shot_name = sanitize_filename(f"{rid}__{sel[:60]}") + ".png"
                        shot_path = (out_dir / "screenshots" / shot_name)
                        shot_saved = crop_element_screenshot(page, sel, shot_path)
                        candidates.append({
                            "page_url": url,
                            "bucket": "semantic_review",
                            "topic": topic,
                            "sc_list": scs,
                            "axe_rule_id": rid,
                            "axe_help": r.get("help"),
                            "axe_help_url": r.get("helpUrl"),
                            "impact": r.get("impact"),
                            "selector": sel,
                            "html_snippet": html_snippet,
                            "attributes": attrs,
                            "role_name_guess": get_role_name_guess(page, sel),
                            "nearby_text": nearby,
                            "acc_snapshot": acc,
                            "screenshot": shot_saved,
                            "failureSummary": n.get("failureSummary"),
                            "why_any": _msgs(n.get("any")),
                            "why_all": _msgs(n.get("all")),
                            "why_none": _msgs(n.get("none")),
                        })
                        break

        (out_dir / "candidates.json").write_text(json.dumps(candidates, indent=2), encoding="utf-8")
        context.close()
        browser.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True, help="Output dir base, e.g., out/run_example")
    args = ap.parse_args()
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_axe_on_url(args.url, out_dir)
