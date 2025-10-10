#!/usr/bin/env python3
# core/axe_runner.py
"""
Playwright runner:
- injects axe-core (robustly), collects axe results (main + same-origin iframes)
- waits for DOM to settle (SPA/lazy content) so we don’t miss nodes
- runs keyboard probe
- runs mechanical/hybrid detectors (imported from detectors.py)
- writes candidates.json (de-duped by (selector, SC), preferring axe)
- builds axe_results.json with axe_issues for Excel 'Overall_Issues'
"""

from __future__ import annotations
import json, os, re, pathlib, argparse
from typing import Any, Dict, Optional, List, Tuple
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page, Frame

# ---------- ensure Playwright browser exists (Cloud-friendly) ----------
def _ensure_playwright_chromium():
    """
    Make sure the Chromium browser binary is available.
    On Streamlit Cloud we can't run build hooks, so install on first run.
    """
    import subprocess, sys, os

    # If Playwright already downloaded browsers, skip
    cache_dir = os.path.expanduser("~/.cache/ms-playwright")
    chromium_dir = os.path.join(cache_dir, "chromium")
    if os.path.exists(chromium_dir):
        return

    # Try to install the Chromium browser binary (OS deps come from packages.txt)
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        # Don't crash the app here—let launch fallback try Firefox below
        pass


# ---- import detectors (all heavy lifting lives there) ----
from .detectors import (
    # shared helpers
    ensure_dir, write_json, sanitize_filename, crop_element_screenshot,
    get_accessibility_snapshot, get_nearby_text, get_role_name_guess, _get_attrs,
    # 2.4.6
    collect_headings_and_labels, evaluate_2_4_6_locally, write_2_4_6_ai_prompt,
    # contrast
    detect_contrast_text_general, detect_contrast_on_image_text, detect_contrast_nontext_ui,
    # other rules
    detect_info_relationships, detect_meaningful_sequence, detect_role_conflicts,
    detect_label_in_name, detect_link_purpose_generic, detect_link_indicator_style, detect_timeouts,
    # focus-visible/focus-order (read from trace)
    detect_focus_visible_weak_from_trace, detect_focus_order_suspect_from_trace,
    # candidate factory used by keyboard probe
    _mk_candidate,
)

# -------------------- config --------------------
AXE_URLS = [
    "https://cdn.jsdelivr.net/npm/axe-core@4.10.0/axe.min.js",
    "https://unpkg.com/axe-core@4.10.0/axe.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.0/axe.min.js",
]

VIEWPORT = {"width": 1440, "height": 1000}
FOCUS_WAIT_MS = 80

# Keyboard probe caps (can be overridden per run)
KB_MAX_STEPS = 200
KB_MAX_REPEATS = 6
KB_MAX_BACK_STEPS = 10
KB_SHOT_DIRNAME = "kb_shots"

# -------------------- tiny helpers --------------------

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

def _norm_sc_from_any(val) -> str:
    if not val:
        return ""
    s = str(val)
    m = re.search(r"(\d)\.(\d)\.(\d)", s)
    if m:
        return ".".join(m.groups())
    m = re.search(r"wcag(\d)(\d)(\d)$", s, re.I)
    return ".".join(m.groups()) if m else ""

# ---------- DOM helpers to avoid missing dynamic content ----------

def wait_for_dom_settle(page: Page, quiet_ms: int = 800, total_timeout: int = 8000):
    """Wait until no DOM mutations for quiet_ms or total_timeout reached."""
    page.evaluate(f"""
      () => new Promise(resolve => {{
        let last = Date.now();
        const obs = new MutationObserver(() => {{ last = Date.now(); }});
        obs.observe(document, {{subtree:true, childList:true, attributes:true, characterData:true}});
        const chk = setInterval(() => {{
          if (Date.now() - last >= {quiet_ms}) {{ clearInterval(chk); obs.disconnect(); resolve(); }}
        }}, 100);
        setTimeout(() => {{ clearInterval(chk); obs.disconnect(); resolve(); }}, {total_timeout});
      }});
    """)

def nudge_lazy_content(page: Page):
    """Scroll to trigger lazy/infinite content."""
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(150)
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(300)
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(150)
    except Exception:
        pass

# ---------- robust axe injection (fixes “axe is not defined”) ----------

def inject_axe(frame: Frame, timeout_ms: int = 6000) -> bool:
    """Try several CDNs; then wait until window.axe.run exists."""
    ok = False
    for u in AXE_URLS:
        try:
            frame.add_script_tag(url=u)
            ok = True
            break
        except Exception:
            continue
    if not ok:
        return False
    try:
        frame.wait_for_function("() => window.axe && typeof axe.run === 'function'", timeout=timeout_ms)
        return True
    except Exception:
        return False

# -------------------- keyboard probe --------------------

def _install_kb_hooks(page) -> None:
    page.evaluate("""() => {
      window.__a11yLastActivation = null;
      const events = ['click','auxclick','submit','change'];
      for (const ev of events) {
        window.addEventListener(ev, () => { window.__a11yLastActivation = {ev, t: Date.now()}; }, true);
      }
      window.__a11ySel = (el) => {
        if (!el) return "";
        const id = el.getAttribute('id');
        if (id) return `#${CSS.escape(id)}`;
        let s = el.tagName.toLowerCase();
        if (el.classList.length) s += '.' + Array.from(el.classList).slice(0,3).map(c=>CSS.escape(c)).join('.');
        const name = el.getAttribute('name');
        if (name) s += `[name="${CSS.escape(name)}"]`;
        return s;
      };
    }""")

def _get_active_info(page) -> Dict[str, Any]:
    return page.evaluate("""() => {
      const el = document.activeElement;
      if (!el) return {selector:"", role:"", name:"", visible:false};
      const cs = getComputedStyle(el);
      const r = (el.getAttribute('role') || el.tagName.toLowerCase());
      let n = el.getAttribute('aria-label') || el.getAttribute('alt') || el.innerText || "";
      if (el.id) {
        const lab = document.querySelector(`[for="${el.id}"]`);
        if (lab) n = (lab.innerText || n);
      }
      const rect = el.getBoundingClientRect();
      const visible = !!(rect && rect.width > 0 && rect.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none');
      return {
        selector: window.__a11ySel ? window.__a11ySel(el) : "",
        role: r, name: (n || "").trim().slice(0,200),
        visible,
        rect: rect ? {x: rect.x, y: rect.y, w: rect.width, h: rect.height} : null,
        outline: cs.outline || "", boxShadow: cs.boxShadow || "", border: `${cs.borderTopWidth} ${cs.borderTopStyle} ${cs.borderTopColor}`
      };
    }""")

def _kb_save_shot(page, step_idx: int, selector: str, out_dir: pathlib.Path, enabled: bool) -> Optional[str]:
    if not enabled:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = sanitize_filename(f"{step_idx:04d}__{selector or 'nofocus'}") + ".png"
    return crop_element_screenshot(page, selector, out_dir / fname, enabled=True) if selector else None

def _press_and_check_activation(page, key: str) -> bool:
    before = page.evaluate("() => ({url: location.href, act: window.__a11yLastActivation})")
    page.keyboard.press(key)
    page.wait_for_timeout(60)
    after = page.evaluate("() => ({url: location.href, act: window.__a11yLastActivation})")
    if after["url"] != before["url"]:
        return True
    if after["act"] and (not before["act"] or after["act"]["t"] != before["act"]["t"]):
        return True
    return False

def _interactive_inventory(page) -> List[Dict[str, Any]]:
    return page.evaluate("""() => {
      const q = (sel) => Array.from(document.querySelectorAll(sel));
      const items = new Set();
      const native = q('a[href], button, input, select, textarea, summary, details[open] > summary');
      native.forEach(el => items.add(el));
      const roles = ['button','link','checkbox','radio','switch','tab','menuitem','option','listbox','combobox','textbox','gridcell','rowheader','columnheader','slider','spinbutton','treeitem'];
      roles.forEach(r => q(`[role="${r}"]`).forEach(el => items.add(el)));
      const res = [];
      const isRovingContainer = (el) => {
        const r = el.getAttribute('role');
        return r === 'tablist' || r === 'menu' || r === 'grid' || r === 'listbox' || r === 'toolbar';
      };
      const inRoving = (el) => {
        let p = el.parentElement;
        while (p) {
          if (isRovingContainer(p)) return true;
          p = p.parentElement;
        }
        return false;
      };
      for (const el of items) {
        const cs = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        const visible = !!(rect && rect.width>0 && rect.height>0 && cs.visibility!=='hidden' && cs.display!=='none');
        const disabledAttr = el.hasAttribute('disabled');
        const ariaDisabled = (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
        res.push({
          selector: window.__a11ySel ? window.__a11ySel(el) : '',
          tag: el.tagName.toLowerCase(),
          role: el.getAttribute('role') || '',
          tabindex: el.getAttribute('tabindex') || '',
          nativeInteractive: !!(el.matches('a[href], button, input, select, textarea')),
          ariaInteractive: !!el.getAttribute('role'),
          inRovingContainer: inRoving(el),
          disabled: !!disabledAttr,
          ariaDisabled: !!ariaDisabled,
          visible
        });
      }
      return res;
    }""")

def run_keyboard_probe(
    page,
    url: str,
    out_dir: pathlib.Path,
    screenshot_elements: bool,
    max_steps: int = KB_MAX_STEPS,
    max_repeats: int = KB_MAX_REPEATS,
    max_back_steps: int = KB_MAX_BACK_STEPS,
    screenshot_keyboard: Optional[bool] = None,   # follows element toggle if None
) -> Dict[str, Any]:
    shots_dir = out_dir / KB_SHOT_DIRNAME
    ensure_dir(shots_dir)
    _install_kb_hooks(page)
    trace_path = out_dir / "keyboard_trace.jsonl"

    if screenshot_keyboard is None:
        screenshot_keyboard = screenshot_elements

    trace, seen_pair_repeats, last_two = [], 0, []

    # forward sweep
    for i in range(1, max_steps+1):
        page.keyboard.press("Tab")
        page.wait_for_timeout(FOCUS_WAIT_MS)
        info = _get_active_info(page)
        sel = info.get("selector") or ""
        shot = _kb_save_shot(page, i, sel, shots_dir, enabled=screenshot_keyboard)
        info["screenshot"] = os.path.join(KB_SHOT_DIRNAME, os.path.basename(shot)) if shot else None
        info["step"] = i
        trace.append(info)
        last_two.append(sel)
        if len(last_two) > 2: last_two.pop(0)
        if len(last_two) == 2 and last_two[0] and last_two[1]:
            if len(trace) >= 4 and trace[-4]["selector"] == last_two[0] and trace[-3]["selector"] == last_two[1]:
                seen_pair_repeats += 1
                if seen_pair_repeats >= max_repeats:
                    break

    # small backward sweep (trap hint)
    back_ok = True
    try:
        for _ in range(min(max_back_steps, len(trace))):
            page.keyboard.down("Shift")
            page.keyboard.press("Tab")
            page.keyboard.up("Shift")
            page.wait_for_timeout(FOCUS_WAIT_MS)
    except Exception:
        back_ok = False

    # unreachable inventory (respect disabled/aria-disabled/roving)
    inventory = _interactive_inventory(page)
    tab_selectors = {t.get("selector","") for t in trace if t.get("selector")}
    unreachable = [
        it for it in inventory
        if it.get("visible")
        and it.get("selector")
        and it["selector"] not in tab_selectors
        and not it.get("disabled")
        and not it.get("ariaDisabled")
        and not it.get("inRovingContainer")
    ]

    # sampled activation checks
    activations = []
    sample_idxs = list(range(0, len(trace), max(1, len(trace)//10)))[:12]
    for idx in sample_idxs:
        sel = trace[idx].get("selector") if idx < len(trace) else None
        if not sel: continue
        el = page.query_selector(sel)
        if not el: continue
        role = el.get_attribute("role") or el.evaluate("el => el.tagName.toLowerCase()")
        if el.evaluate("(el)=>el.tagName.toLowerCase()==='a' && el.hasAttribute('href')"):
            continue
        page.focus(sel)
        page.wait_for_timeout(FOCUS_WAIT_MS)
        enter_ok = _press_and_check_activation(page, "Enter")
        space_ok = _press_and_check_activation(page, "Space")
        activations.append({
            "selector": sel,
            "role": role,
            "enter_ok": bool(enter_ok),
            "space_ok": bool(space_ok),
        })

    trap_suspected = (seen_pair_repeats >= max_repeats) and (not back_ok)

    # persist
    with (trace_path).open("w", encoding="utf-8") as f:
        for row in trace:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "tab_stops": len(tab_selectors),
        "trace_steps": len(trace),
        "trap_suspected": bool(trap_suspected),
        "activation_checks": len(activations),
        "unreachable": len(unreachable),
    }
    probe = {
        "summary": summary,
        "unreachable": unreachable,
        "activations": activations,
        "tabindex_neg1": [
            it for it in inventory
            if str(it.get("tabindex","")).strip() == "-1"
               and (it.get("nativeInteractive") or it.get("ariaInteractive"))
               and not it.get("inRovingContainer")
               and not it.get("disabled")
               and not it.get("ariaDisabled")
        ]
    }
    write_json(out_dir / "keyboard_probe.json", probe)
    return probe

# -------------------- frames, DOM, axe --------------------

def for_each_same_origin_frame(page, include_frames: bool):
    yield page.main_frame
    if not include_frames:
        return
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        try:
            fr.evaluate("() => 1")  # same-origin probe
            yield fr
        except Exception:
            continue

def save_dom_snapshots(page, out_dir: pathlib.Path, include_frames: bool) -> None:
    dom_dir = out_dir / "dom"
    ensure_dir(dom_dir)
    idx = []
    i = 0
    for fr in for_each_same_origin_frame(page, include_frames=include_frames):
        try:
            url = fr.url
            doctype = fr.evaluate("""() => {
              const dt = document.doctype;
              return dt ? `<!DOCTYPE ${dt.name}${dt.publicId ? ` PUBLIC "${dt.publicId}"` : ""}${dt.systemId ? ` "${dt.systemId}"` : ""}>\\n` : "";
            }""")
            html = fr.content()
            name = f"frame_{i}.html" if fr != page.main_frame else "main.html"
            (dom_dir / name).write_text(doctype + html, encoding="utf-8")
            idx.append({"name": name, "url": url, "is_main": fr == page.main_frame})
        except Exception:
            idx.append({"name": None, "url": getattr(fr, "url", "unknown"), "is_main": fr == page.main_frame, "error": "unreachable"})
        i += 1
    write_json(dom_dir / "index.json", idx)

def run_axe_all_frames(page, include_frames: bool, result_types: Optional[List[str]] = None) -> Dict[str,Any]:
    """Assumes axe is injected & available in all frames. We call inject_axe before this."""
    all_results = {"violations": [], "incomplete": [], "passes": []}
    def _merge(res):
        for k in ("violations","incomplete","passes"):
            all_results[k].extend(res.get(k, []))

    axe_opts = {
        "runOnly": {"type":"tag","values":["wcag2a","wcag2aa","wcag21a","wcag21aa","wcag22a","wcag22aa"]},
        "resultTypes": result_types or ["violations","incomplete","passes"]
    }

    # main
    _merge(page.evaluate("(opts)=>axe.run(document, opts)", axe_opts))

    # frames
    if include_frames:
        for fr in for_each_same_origin_frame(page, include_frames=True):
            if fr == page.main_frame:
                continue
            try:
                _merge(fr.evaluate("(opts)=>axe.run(document, opts)", axe_opts))
            except Exception:
                continue
    return all_results

# -------------------- issues normalization --------------------

def _mk_issue_from_axe_node(url: str, rule: Dict[str,Any], node: Dict[str,Any], bucket: str) -> Dict[str,Any]:
    scs = []
    for t in rule.get("tags", []) or []:
        m = re.match(r"wcag(\d)(\d)(\d)$", str(t).lower())
        if m: scs.append(".".join(m.groups()))
    status = "fail" if bucket=="violations" else ("review" if bucket=="incomplete" else "pass")
    return {
        "page_url": url,
        "SC": scs[0] if scs else "",
        "status": status,                         # pass | fail | review
        "rule_id": rule.get("id"),
        "impact": rule.get("impact"),
        "selector": (node.get("target") or [""])[0],
        "screenshot": None,
        "note": node.get("failureSummary"),
        "detector": "axe",
        "source": bucket,
        "axe_help_url": rule.get("helpUrl"),
        "evidence": {
            "why_any": _msgs(node.get("any")),
            "why_all": _msgs(node.get("all")),
            "why_none": _msgs(node.get("none")),
            "html": node.get("html")
        }
    }

def _mk_issue_from_candidate(c: Dict[str,Any]) -> Dict[str,Any]:
    sc = c.get("SC") or _norm_sc_from_any(c.get("topic")) or _norm_sc_from_any(c.get("axe_rule_id"))
    rule_id = c.get("axe_rule_id") or c.get("rule_id") or (c.get("detector") or c.get("source") or "detector")
    status = (c.get("verdict") or "review").lower()
    return {
        "page_url": c.get("page_url"),
        "SC": sc,
        "status": status,                           # pass | fail | review
        "rule_id": rule_id,
        "impact": c.get("impact"),
        "selector": c.get("selector"),
        "screenshot": c.get("screenshot"),
        "note": c.get("reason") or c.get("note") or c.get("failureSummary"),
        "detector": c.get("source") or "detector",
        "source": c.get("source") or "detector",
        "axe_help_url": c.get("axe_help_url"),
        "evidence": {
            "why_any": c.get("why_any"),
            "why_all": c.get("why_all"),
            "why_none": c.get("why_none"),
            "html": c.get("html_snippet")
        }
    }

def _dedupe_issues(issues: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    seen, out = set(), []
    for i in issues:
        key = (i.get("rule_id"), i.get("selector"), i.get("status"))
        if key in seen:
            continue
        seen.add(key)
        out.append(i)
    return out

# -------------------- de-dup candidates (selector + SC) --------------------

def _primary_sc_from(c: Dict[str,Any]) -> str:
    for k in ("SC","sc"):
        if k in c and c[k]:
            return str(c[k])
    for s in (c.get("sc_list") or []):
        m = re.search(r"(\d)\.(\d)\.(\d)", str(s))
        if m: return ".".join(m.groups())
    for k in ("topic","axe_rule_id","rule_id"):
        m = re.search(r"(\d)\.(\d)\.(\d)", str(c.get(k,"")))
        if m: return ".".join(m.groups())
    return ""

def _cand_score(c: Dict[str,Any]) -> int:
    """Higher score = keep. Prefer axe-based records (richer diagnostics)."""
    s = 0
    if c.get("axe_rule_id"): s += 2
    if c.get("failureSummary"): s += 2
    if (c.get("why_any") or c.get("why_all") or c.get("why_none")): s += 1
    if c.get("bucket") == "must_review": s += 1
    rid = (c.get("axe_rule_id") or c.get("rule_id") or "")
    if isinstance(rid, str) and ("keyboard-probe" in rid or "1.4.3" in rid or "1.4.11" in rid):
        s += 1
    return s

def _dedupe_by_selector_keep_best(cands: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    """
    Keep the highest-scoring row per (selector, primary SC).
    Preserves distinct WCAG findings for the same element, but within each SC bucket
    prefers the axe-based one.
    """
    best: Dict[Tuple[str,str], Dict[str,Any]] = {}
    nosel: List[Dict[str,Any]] = []
    for c in cands:
        sel = (c.get("selector") or "").strip()
        if not sel:
            nosel.append(c)
            continue
        sc = _primary_sc_from(c)
        key = (sel, sc)
        cur = best.get(key)
        if cur is None or _cand_score(c) > _cand_score(cur):
            best[key] = c
    return list(best.values()) + nosel

# -------------------- main run --------------------

def run_axe_on_url(
    url: str,
    out_dir: pathlib.Path,
    timeout_ms: int = 30000,
    fast_mode: bool = False,
    include_frames: bool = True,
    capture_dom: bool = True,
    screenshot_elements: bool = True,
    quick_scan: bool = False,
    ultra_quick: bool = False,
    kb_steps_override: Optional[int] = None,
    kb_repeats_override: Optional[int] = None,
    kb_back_steps_override: Optional[int] = None,
    enable_contrast_checks: bool = True,
    quick_screenshots: Optional[str] = None,
    screenshot_keyboard: Optional[bool] = None,   # keyboard screenshots follow element toggle if None
) -> None:
    """
    Modes:
      - fast_mode: skips iframes & DOM snapshots. Keyboard still runs.
      - quick_scan: fewer keyboard steps, no contrast, screenshots off (or fail-only with quick_screenshots="fail-only").
      - ultra_quick: axe violations only (main doc), no keyboard, DOM, contrast, 2.4.6, or extra detectors.
    """
    out_dir = pathlib.Path(out_dir)
    ensure_dir(out_dir)
    ensure_dir(out_dir / "screenshots")

    # normalize fast mode
    if fast_mode:
        include_frames = False
        capture_dom = False

    # profiles
    if ultra_quick:
        include_frames = False
        capture_dom = False
        screenshot_elements = False
        quick_scan = False
        axe_result_types = ["violations"]
        skip_keyboard = True
        skip_extra_detectors = True
        skip_ai_246 = True
        kb_steps = kb_repeats = kb_back = 0
        enable_contrast_checks = False
    else:
        axe_result_types = None
        skip_keyboard = False
        skip_extra_detectors = False
        skip_ai_246 = False
        if quick_scan:
            screenshot_elements = False
            enable_contrast_checks = False
            kb_steps = kb_steps_override or 80
            kb_repeats = kb_repeats_override or 3
            kb_back = kb_back_steps_override or 6
            if quick_screenshots == "fail-only":
                screenshot_elements = True
        else:
            kb_steps = kb_steps_override or KB_MAX_STEPS
            kb_repeats = kb_repeats_override or KB_MAX_REPEATS
            kb_back = kb_back_steps_override or KB_MAX_BACK_STEPS

    write_json(out_dir / "metadata.json", {
        "page_url": url,
        "axe_version_requested": "4.10.0",
        "fast_mode": bool(fast_mode),
        "include_frames": bool(include_frames),
        "capture_dom": bool(capture_dom),
        "screenshot_elements": bool(screenshot_elements),
        "screenshot_keyboard": (screenshot_keyboard if screenshot_keyboard is not None else screenshot_elements),
        "quick_scan": bool(quick_scan),
        "ultra_quick": bool(ultra_quick),
    })

    with sync_playwright() as p:
        # Ensure browser binary present on first run (Cloud)
        _ensure_playwright_chromium()

        # Container-friendly launch flags
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
        ]

        browser = None
        context = None
        try:
            # Try Chromium first, then Firefox fallback
            try:
                browser = p.chromium.launch(headless=True, args=launch_args)
            except Exception:
                browser = p.firefox.launch(headless=True)

            context = browser.new_context(viewport=VIEWPORT)
            page = context.new_page()

            # ---------- navigation (robust) ----------
            context.set_default_navigation_timeout(timeout_ms)
            context.set_default_timeout(timeout_ms)

            def _should_block(route):
                url_ = route.request.url
                return any(s in url_ for s in (
                    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
                    "facebook.net", "hotjar.com", "segment.com", "clarity.ms", "optimizely.com",
                    "fontawesome.com", "cdn.jsdelivr.net/npm/font-awesome", ".woff", ".woff2"
                ))

            try:
                page.route("**/*", lambda route: route.abort() if _should_block(route) else route.continue_())
            except Exception:
                pass

            try:
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            except PlaywrightTimeoutError:
                try:
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("load", timeout=max(2000, timeout_ms // 2))
                    except PlaywrightTimeoutError:
                        pass
                except PlaywrightTimeoutError:
                    try:
                        page.goto(url, timeout=timeout_ms, wait_until="load")
                    except PlaywrightTimeoutError:
                        pass

            # expand & settle
            page.wait_for_timeout(400 if (fast_mode or quick_scan) else 800)
            page.evaluate("""() => {
              document.querySelectorAll('details:not([open])').forEach(d => { try { d.open = true; } catch(e){} });
              Array.from(document.querySelectorAll('[aria-expanded="false"]')).slice(0,50).forEach(el => { try { el.click(); } catch(e){} });
              Array.from(document.querySelectorAll('[role="tab"]')).slice(0,6).forEach(t => { try { t.click(); } catch(e){} });
            }""")
            page.wait_for_timeout(250 if (fast_mode or quick_scan) else 400)

            # nudge lazy content + wait for DOM settle
            nudge_lazy_content(page)
            wait_for_dom_settle(page, quiet_ms=800, total_timeout=8000)

            # inject axe in main + frames, and verify availability
            if not inject_axe(page.main_frame):
                raise RuntimeError("Failed to inject axe-core into main document.")
            if include_frames:
                for fr in for_each_same_origin_frame(page, include_frames=True):
                    if fr == page.main_frame:
                        continue
                    try:
                        inject_axe(fr)
                    except Exception:
                        continue

            # DOM snapshots (optional)
            if capture_dom and not ultra_quick:
                save_dom_snapshots(page, out_dir, include_frames=include_frames)

            # axe across frames (we already injected axe)
            axe_payload = run_axe_all_frames(page, include_frames=include_frames, result_types=axe_result_types)
            write_json(out_dir / "axe_results.json", axe_payload)

            # node-by-node debug
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

            # build candidates from axe (violations + incomplete)
            candidates: List[Dict[str, Any]] = []
            seen = set()
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
                        attrs = _get_attrs(page, selector)
                        shot_name = sanitize_filename(f"{r.get('id')}__{selector[:60]}") + ".png"
                        shot_path = (out_dir / "screenshots" / shot_name)
                        shot_saved = crop_element_screenshot(page, selector, shot_path, enabled=screenshot_elements)
                        candidates.append({
                            "source": "axe",
                            "page_url": url,
                            "bucket": "must_review",
                            "topic": topic,
                            "sc_list": scs,
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
                            "failureSummary": n.get("failureSummary"),
                            "why_any": _msgs(n.get("any")),
                            "why_all": _msgs(n.get("all")),
                            "why_none": _msgs(n.get("none")),
                            "verdict": "fail"
                        })

            # keyboard probe
            if not ultra_quick:
                kb = run_keyboard_probe(
                    page, url, out_dir,
                    screenshot_elements=screenshot_elements,
                    max_steps=(80 if quick_scan else KB_MAX_STEPS),
                    max_repeats=(3 if quick_scan else KB_MAX_REPEATS),
                    max_back_steps=(6 if quick_scan else KB_MAX_BACK_STEPS),
                    screenshot_keyboard=screenshot_keyboard,
                )
                for it in kb.get("unreachable", [])[:40]:
                    sel = it.get("selector")
                    cand = _mk_candidate(page, url, "2.1.1", "keyboard-probe:unreachable", sel, "Interactive element appears unreachable by Tab.", verdict="fail")
                    shot_path = (out_dir / "screenshots" / (sanitize_filename(f"kb_unreach__{(sel or '')[:60]}") + ".png"))
                    cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                    cand["source"] = "keyboard"
                    candidates.append(cand)
                for it in kb.get("activations", [])[:30]:
                    if not (it.get("enter_ok") or it.get("space_ok")):
                        sel = it.get("selector")
                        cand = _mk_candidate(page, url, "2.1.1", "keyboard-probe:activation", sel, "Enter/Space did not activate a focusable control.", verdict="fail")
                        shot_path = (out_dir / "screenshots" / (sanitize_filename(f"kb_act__{(sel or '')[:60]}") + ".png"))
                        cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                        cand["source"] = "keyboard"
                        candidates.append(cand)
                for it in kb.get("tabindex_neg1", [])[:30]:
                    sel = it.get("selector")
                    cand = _mk_candidate(page, url, "2.1.1", "keyboard-probe:tabindex--1", sel, "Interactive element with tabindex='-1' (roving/disabled exceptions handled).", verdict="fail")
                    shot_path = (out_dir / "screenshots" / (sanitize_filename(f"kb_tabneg__{(sel or '')[:60]}") + ".png"))
                    cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                    cand["source"] = "keyboard"
                    candidates.append(cand)

                # weak focus-visible + focus-order (from trace)
                try:
                    fv = detect_focus_visible_weak_from_trace(page, url, out_dir, screenshot_elements=screenshot_elements)
                    for c in fv: c.setdefault("source","keyboard-trace")
                    candidates.extend(fv)
                    fo = detect_focus_order_suspect_from_trace(page, url, out_dir, screenshot_elements=screenshot_elements)
                    for c in fo: c.setdefault("source","keyboard-trace")
                    candidates.extend(fo)
                except Exception:
                    pass

            # additional detectors (skip in ultra_quick)
            if not ultra_quick:
                for det in (
                    lambda pg,u,od: detect_info_relationships(pg,u,od,screenshot_elements),   # 1.3.1
                    lambda pg,u,od: detect_meaningful_sequence(pg,u,od,screenshot_elements),  # 1.3.2
                    lambda pg,u,od: detect_role_conflicts(pg,u,od,screenshot_elements),       # 4.1.2
                    lambda pg,u,od: detect_label_in_name(pg,u,od,screenshot_elements),        # 2.5.3
                    lambda pg,u,od: detect_link_purpose_generic(pg,u,od,screenshot_elements), # 2.4.4
                    lambda pg,u,od: detect_link_indicator_style(pg,u,od,screenshot_elements), # 1.4.1
                    detect_timeouts,                                                         # 2.2.6
                ):
                    try:
                        rows = det(page, url, out_dir)
                        if isinstance(rows, list):
                            for c in rows:
                                c.setdefault("source", "detector")
                            candidates.extend(rows)
                    except Exception:
                        pass

            # contrast (optional)
            if not ultra_quick and enable_contrast_checks:
                c143_fail_a, c143_pass_a = detect_contrast_text_general(page, url, out_dir)
                c143_fail_b, c143_pass_b = detect_contrast_on_image_text(page, url, out_dir)
                c1411_fail,  c1411_pass  = detect_contrast_nontext_ui(page, url, out_dir)
                for c in (c143_fail_a + c143_fail_b + c1411_fail):
                    c.setdefault("source","detector-contrast")
                candidates.extend(c143_fail_a + c143_fail_b + c1411_fail)
                write_json(out_dir / "rule_pages" / "1.4.3_passes.json", c143_pass_a + c143_pass_b)
                write_json(out_dir / "rule_pages" / "1.4.11_passes.json", c1411_pass)

            # 2.4.6: collect + local heuristic + AI prompt
            if not ultra_quick:
                collected = []
                for fr in for_each_same_origin_frame(page, include_frames=include_frames):
                    try:
                        items = collect_headings_and_labels(fr)
                        src = "main" if fr == page.main_frame else "frame"
                        for it in items:
                            it["source"] = src
                        collected.extend(items)
                    except Exception:
                        continue
                write_json(out_dir / "ai" / "2_4_6" / "input.json", collected)
                write_2_4_6_ai_prompt(out_dir, url, fast_mode=fast_mode)
                fails_246, passes_246 = evaluate_2_4_6_locally(collected)
                for it in fails_246:
                    sel = it.get("selector")
                    reason = "; ".join(it.get("reasons", [])) or "Non-descriptive heading/label."
                    cand = _mk_candidate(page, url, "2.4.6", "runner:ai-2.4.6-local", sel, reason, verdict="fail", evidence=it)
                    shot_path = (out_dir / "screenshots" / (sanitize_filename(f"sc246__{(sel or '')[:60]}") + ".png"))
                    cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                    cand["source"] = "runner-2.4.6-local"
                    candidates.append(cand)
                write_json(out_dir / "rule_pages" / "2.4.6_passes.json", passes_246)

            # -------- de-duplicate candidates & persist --------
            write_json(out_dir / "candidates_raw.json", candidates)
            candidates = _dedupe_by_selector_keep_best(candidates)
            write_json(out_dir / "candidates.json", candidates)

            # -------- BUILD unified axe_issues for Excel: Overall_Issues --------
            axe_issues: List[Dict[str,Any]] = []

            # A) axe buckets → issues
            for bucket in ("violations","incomplete","passes"):
                for r in axe_payload.get(bucket, []):
                    for n in r.get("nodes", []) or []:
                        axe_issues.append(_mk_issue_from_axe_node(url, r, n, bucket))

            # B) all candidates (mostly 'fail' from axe/detectors/keyboard)
            for c in candidates:
                axe_issues.append(_mk_issue_from_candidate(c))

            # C) optional detector PASSES (contrast & 2.4.6) → issues as 'pass'
            try:
                c143_pass_all = []
                try:
                    c143_pass_all.extend(json.loads((out_dir / "rule_pages" / "1.4.3_passes.json").read_text(encoding="utf-8")))
                except Exception:
                    pass
                c1411_pass = []
                try:
                    c1411_pass.extend(json.loads((out_dir / "rule_pages" / "1.4.11_passes.json").read_text(encoding="utf-8")))
                except Exception:
                    pass

                for row in c143_pass_all:
                    row = dict(row)
                    row["verdict"] = "pass"
                    row["rule_id"] = row.get("rule_id") or "detector:1.4.3"
                    row["source"]  = row.get("source") or "detector-contrast"
                    axe_issues.append(_mk_issue_from_candidate(row))

                for row in c1411_pass:
                    row = dict(row)
                    row["verdict"] = "pass"
                    row["rule_id"] = row.get("rule_id") or "detector:1.4.11"
                    row["source"]  = row.get("source") or "detector-contrast"
                    axe_issues.append(_mk_issue_from_candidate(row))
            except Exception:
                pass

            # D) 2.4.6 passes (local heuristic)
            try:
                passes_246 = json.loads((out_dir / "rule_pages" / "2.4.6_passes.json").read_text(encoding="utf-8"))
                for row in passes_246 or []:
                    row = dict(row)
                    row["verdict"] = "pass"
                    row["rule_id"] = row.get("rule_id") or "runner:ai-2.4.6-local"
                    row["source"]  = row.get("source") or "runner-2.4.6-local"
                    axe_issues.append(_mk_issue_from_candidate(row))
            except Exception:
                pass

            # E) de-dupe & write axe_results with issues + raw
            axe_issues = _dedupe_issues(axe_issues)
            write_json(out_dir / "axe_results.json", {
                "axe_raw": axe_payload,
                "axe_issues": axe_issues
            })

        finally:
            # Always clean up
            try:
                if context: context.close()
            except Exception:
                pass
            try:
                if browser: browser.close()
            except Exception:
                pass

# -------------------- CLI --------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True, help="Output dir base, e.g., out/run_example")
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--fast-mode", action="store_true", help="Skip iframes & DOM snapshots (keyboard still runs)")
    ap.add_argument("--no-frames", action="store_true", help="Do not scan same-origin iframes")
    ap.add_argument("--no-dom", action="store_true", help="Do not save DOM snapshots")
    ap.add_argument("--no-element-screens", action="store_true", help="Do not capture element crops")
    ap.add_argument("--quick-scan", action="store_true", help="Faster profile: fewer keyboard steps, no contrast, screenshots off (or fail-only via --quick-screenshots).")
    ap.add_argument("--ultra-quick", action="store_true", help="Axe-only violations on main doc. Skips keyboard/contrast/2.4.6/DOM.")
    ap.add_argument("--quick-screenshots", choices=["fail-only"], default=None)
    ap.add_argument("--no-keyboard-screens", action="store_true", help="Disable keyboard (Tab trace) screenshots regardless of element screenshot setting.")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    run_axe_on_url(
        url=args.url,
        out_dir=out_dir,
        timeout_ms=args.timeout_ms,
        fast_mode=args.fast_mode,
        include_frames=not args.fast_mode and not args.no_frames,
        capture_dom=not args.fast_mode and not args.no_dom,
        screenshot_elements=not args.no_element_screens,
        quick_scan=args.quick_scan,
        ultra_quick=args.ultra_quick,
        quick_screenshots=args.quick_screenshots,
        screenshot_keyboard=(False if args.no_keyboard_screens else None),
    )
