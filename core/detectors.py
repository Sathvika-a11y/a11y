#!/usr/bin/env python3
# core/detectors.py
# Targeted improvements per validation report; function names/exports unchanged.

from __future__ import annotations
import json, os, re, pathlib
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image

# -------------------- small utils --------------------

def ensure_dir(p: pathlib.Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def write_json(path: pathlib.Path, obj: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")

def sanitize_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s or "")
    return s[:120]

def crop_element_screenshot(page, selector: str, out_path: pathlib.Path, enabled: bool=True) -> Optional[str]:
    if not enabled:
        return None
    try:
        el = page.query_selector(selector)
        if not el:
            return None
        try:
            el.screenshot(path=str(out_path))
            return str(out_path)
        except Exception:
            pass
        box = el.bounding_box()
        if not box:
            return None
        clip = {"x": max(0, box["x"] - 2), "y": max(0, box["y"] - 2), "width": box["width"] + 4, "height": box["height"] + 4}
        tmp = out_path.parent / (out_path.stem + "_full.png")
        page.screenshot(path=str(tmp), full_page=True)
        im = Image.open(tmp)
        x = int(clip["x"]); y = int(clip["y"]); w = int(clip["width"]); h = int(clip["height"])
        w = max(0, min(w, im.width - x)); h = max(0, min(h, im.height - y))
        if w <= 0 or h <= 0:
            return None
        im.crop((x, y, x+w, y+h)).save(out_path)
        try:
            tmp.unlink()
        except Exception:
            pass
        return str(out_path)
    except Exception:
        return None

# -------------------- DOM helpers for candidates --------------------

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
      const parent = el.closest('figure, a, button, label, td, th, p, div, section, article, li') || el.parentElement || document.body;
      const text = parent.innerText || "";
      return text.trim().slice(0, 600);
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
      function textOf(n){ return (n && (n.innerText || n.textContent) || "").trim(); }
      function byIds(ids){ return ids.map(id => document.getElementById(id)).filter(Boolean).map(textOf).join(" ").trim(); }
      const ariaLabel = el.getAttribute('aria-label') || "";
      const byLabel = (el.getAttribute('aria-labelledby') || "").split(/\\s+/).filter(Boolean);
      const labelled = byIds(byLabel);
      let fromLabelEl = "";
      if (el.id) {
        const lab = document.querySelector(`[for="${CSS.escape(el.id)}"]`);
        if (lab) fromLabelEl = textOf(lab);
      }
      const inline = textOf(el);
      const name = (ariaLabel || labelled || fromLabelEl || inline || "").trim();
      return (role + " — " + name).slice(0, 250);
    }"""
    try:
        return page.evaluate(js, selector) or ""
    except Exception:
        return ""

def _get_attrs(page, selector: str) -> Dict[str, Any]:
    try:
        if not page.query_selector(selector):
            return {}
        return page.eval_on_selector(
            selector,
            "(el)=>{const o={};for (const a of el.getAttributeNames()) o[a]=el.getAttribute(a); return o;}"
        )
    except Exception:
        return {}

# -------------------- candidate factory --------------------

def _mk_candidate(
    page,
    url: str,
    topic_sc: str,
    det_id: str,
    selector: Optional[str],
    note: str,
    verdict: str="fail",
    evidence: Optional[dict]=None
) -> Dict[str, Any]:
    html_snippet, attrs, acc, nearby, role_name = "", {}, {}, "", ""
    try:
        if selector:
            el = page.query_selector(selector)
            html_snippet = el.evaluate("el=>el.outerHTML.slice(0,2000)") if el else ""
            attrs = _get_attrs(page, selector)
            acc = get_accessibility_snapshot(page, selector)
            nearby = get_nearby_text(page, selector)
            role_name = get_role_name_guess(page, selector)
    except Exception:
        pass
    return {
        "page_url": url,
        "bucket": "semantic_review",
        "topic": f"SC-{topic_sc}" if topic_sc else "BEST_PRACTICE",
        "sc_list": [topic_sc] if topic_sc else [],
        "axe_rule_id": f"{det_id}",
        "axe_help": note,
        "axe_help_url": "",
        "impact": "moderate",
        "selector": selector or "",
        "html_snippet": html_snippet,
        "attributes": attrs,
        "role_name_guess": role_name,
        "nearby_text": nearby,
        "acc_snapshot": acc,
        "screenshot": None,
        "failureSummary": note if verdict=="fail" else None,
        "why_any": [],
        "why_all": [note],
        "why_none": [],
        "verdict": verdict.lower(),
        "evidence": evidence or {}
    }

# -------------------- 2.4.6 collectors/heuristic/prompt (unchanged) --------------------

GENERIC_HEADINGS = {"learn more","more","details","products","features","resources","solutions","explore","get started"}
GENERIC_LABELS  = {"learn more","more","details","click here","submit","go","ok"}

def _norm_text(t: str) -> str:
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^\w\s]", "", t)
    return t

def collect_headings_and_labels(frame) -> List[Dict[str,Any]]:
    return frame.evaluate("""() => {
      const out = [];
      const sel = el => {
        if (!el) return "";
        const id = el.getAttribute('id'); if (id) return `#${CSS.escape(id)}`;
        let s = el.tagName.toLowerCase();
        if (el.classList.length) s += '.' + Array.from(el.classList).slice(0,3).map(c=>CSS.escape(c)).join('.');
        return s;
      };
      const textOf = n => (n && (n.innerText || n.textContent) || "").trim();
      const regionOf = (el) => {
        let r = el.closest('main,nav,aside,header,footer');
        return r ? r.tagName.toLowerCase() : 'body';
      };
      const heads = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]')];
      for (const h of heads) {
        const lvl = h.tagName.match(/^H([1-6])$/) ? +RegExp.$1 : +(h.getAttribute('aria-level') || 0);
        if (!lvl) continue;
        out.push({ type: 'heading', selector: sel(h), level: lvl, visibleText: textOf(h), region: regionOf(h), nearbyText: textOf(h.closest('section,article,div')) });
      }
      const ctrls = [...document.querySelectorAll('button,a[href],input,select,textarea,[role="button"],[role="link"]')];
      for (const el of ctrls) {
        const rect = el.getBoundingClientRect();
        if (!(rect.width>16 && rect.height>12)) continue;
        const role = el.getAttribute('role') || el.tagName.toLowerCase();
        let name = el.getAttribute('aria-label') || "";
        if (!name) {
          const labelledby = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
          if (labelledby.length) {
            name = labelledby.map(id => (document.getElementById(id)?.innerText || "")).join(" ").trim();
          }
        }
        if (!name) name = textOf(el);
        let vis = "";
        if (el.id) {
          const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (lab) vis = textOf(lab);
        }
        if (!vis) vis = textOf(el);
        out.push({ type: 'label', selector: sel(el), controlType: role, visibleLabel: vis, accessibleName: name, region: regionOf(el), nearbyText: textOf(el.closest('form,section,article,div')) });
      }
      return out;
    }""")

def detect_bypass_blocks(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str,Any]]:
    """
    SC 2.4.1 — Provide a mechanism to bypass blocks of repeated content.
    PASS if there is:
      - A visible *skip* link (anchor to an in-page target) OR
      - A <main> landmark (or role="main")
    If neither is present -> MANUAL_REVIEW (conservative).
    """
    js = """() => {
      const q = (sel)=>Array.from(document.querySelectorAll(sel));
      const hasMain = !!document.querySelector('main, [role="main"]');
      const skip = q('a[href^="#"]').some(a => {
        const t = (a.innerText || a.textContent || '').toLowerCase();
        return /skip/.test(t);
      });
      return { hasMain, hasSkip: skip };
    }"""
    out = []
    try:
        r = page.evaluate(js)
        if r and (r.get("hasMain") or r.get("hasSkip")):
            return out
        cand = _mk_candidate(
            page, url, "2.4.1", "runner:bypass-blocks", "",
            "No landmark <main>/role=main or obvious skip link found — verify bypass mechanism.",
            verdict="manual_review"
        )
        out.append(cand)
    except Exception:
        pass
    return out

def detect_language_of_page(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str,Any]]:
    """
    SC 3.1.1 — Language of Page.
    FAIL if <html> has no lang or an empty/unknown value.
    """
    js = """() => {
      const html = document.documentElement;
      const lang = (html.getAttribute('lang') || '').trim();
      return { lang };
    }"""
    out = []
    try:
        r = page.evaluate(js)
        lang = (r.get("lang") or "").strip().lower()
        if not lang or lang in ("xx", "und", "none"):
            cand = _mk_candidate(
                page, url, "3.1.1", "runner:language-of-page", "html",
                "<html> element missing valid lang attribute.", verdict="fail"
            )
            out.append(cand)
    except Exception:
        pass
    return out

def evaluate_2_4_6_locally(items: List[Dict[str,Any]]) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    """
    Only check:
      - Headings
      - Labels for form inputs (NOT CTA buttons or generic links)
    """
    fails, passes = [], []
    for it in items:
        if it["type"] == "heading":
            txt = _norm_text(it.get("visibleText",""))
            bad = (not txt) or (len(txt) <= 2) or (txt in GENERIC_HEADINGS)
            verdict = "fail" if bad else "pass"
            (fails if verdict=="fail" else passes).append({
                **it, "sc": "2.4.6", "verdict": verdict,
                "reasons": ["Heading text generic or empty"] if verdict=="fail" else []
            })
        else:
            role = (it.get("controlType") or "").lower()
            if role in ("button","a","link","[role=\"button\"]","[role=\"link\"]"):
                continue
            vis = _norm_text(it.get("visibleLabel",""))
            name = _norm_text(it.get("accessibleName",""))
            generic = (vis in GENERIC_LABELS) or (not vis and name in GENERIC_LABELS)
            bad = generic or (vis and len(vis) <= 2)
            verdict = "fail" if bad else "pass"
            (fails if verdict=="fail" else passes).append({
                **it, "sc": "2.4.6", "verdict": verdict,
                "reasons": ["Label generic/too short"] if verdict=="fail" else []
            })
    return fails, passes

def write_2_4_6_ai_prompt(out_dir: pathlib.Path, url: str, fast_mode: bool) -> None:
    scope = "MAIN document only (fast mode; no iframes)" if fast_mode else "main document PLUS same-origin iframes"
    prompt = f"""You are an accessibility auditor focused on WCAG 2.4.6 (Headings and Labels).
Determine whether each heading/label clearly communicates PURPOSE or ACTION, using only given context.

Scope: Items extracted from {scope}.
Use 'nearbyText' and 'region' as primary evidence. Do not invent context.

Verdicts:
- PASS: specific & purpose-revealing for its context
- FAIL: generic/ambiguous (“learn more”, “click here”, “more”, “submit”, “ok”, etc.)
- REVIEW: borderline — context may suffice but evidence is thin
- If length ≤2 chars, usually FAIL unless icon+ARIA conveys purpose → REVIEW

Return STRICT JSON...
"""
    write_json(out_dir / "ai" / "2_4_6" / "prompt.txt", {"prompt": prompt})

# -------------------- contrast helpers + detectors --------------------

CONTRAST_SAMPLE_GRID = 3
CONTRAST_MIN_NORMAL = 4.5       # SC 1.4.3
CONTRAST_MIN_LARGE  = 3.0       # SC 1.4.3 (large/bold)
CONTRAST_NON_TEXT_UI = 3.0      # SC 1.4.11

_INTERACTIVE_ROLES = {"button","link","tab","switch","checkbox","radio","menuitem","option"}
def _is_interactive_role(role: str) -> bool:
    return (role or "").lower() in _INTERACTIVE_ROLES

def _relative_lum(rgb):
    def lin(c):
        c = c/255.0
        return (c/12.92) if (c<=0.03928) else (((c+0.055)/1.055)**2.4)
    r,g,b = rgb
    return 0.2126*lin(r) + 0.7152*lin(g) + 0.0722*lin(b)

def _contrast_ratio(fg_rgb, bg_rgb):
    L1 = _relative_lum(fg_rgb)
    L2 = _relative_lum(bg_rgb)
    Lmax, Lmin = (max(L1,L2), min(L1,L2))
    return (Lmax + 0.05) / (Lmin + 0.05)

def _parse_rgba_any(s: str):
    nums = re.findall(r"[\d.]+", s or "")
    if len(nums) >= 3:
        r,g,b = [int(float(nums[i])) for i in range(3)]
        a = float(nums[3]) if len(nums)>=4 else 1.0
        return (r,g,b,a)
    return (0,0,0,1.0)

def _sample_bg_from_image(im: Image.Image, rect) -> Tuple[int,int,int]:
    cx = int(rect["x"] + rect["w"]/2)
    cy = int(rect["y"] + rect["h"]/2)
    samples = []
    for dx in range(-CONTRAST_SAMPLE_GRID//2, CONTRAST_SAMPLE_GRID//2 + 1):
        for dy in range(-CONTRAST_SAMPLE_GRID//2, CONTRAST_SAMPLE_GRID//2 + 1):
            x = min(max(0, cx + dx*3), im.width-1)
            y = min(max(0, cy + dy*3), im.height-1)
            samples.append(im.getpixel((x, y))[:3])
    return tuple(sum(c[i] for c in samples)//len(samples) for i in (0,1,2))

# -------------------- 1.1.1 Non-text content --------------------

def detect_non_text_content_111(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 1.1.1 — Mechanical checks.
      A) <img> without alt (and not decorative) -> FAIL
      B) <input type="image"> without alt      -> FAIL
      C) Focusable/interactive <img> with empty alt ("") -> FAIL
      D) *Graphics only*: <svg> or role="img" lacking an accessible name (no <title>/aria-*) -> FAIL
         (DO NOT fail native <button> elements if they have visible text or an accname)
      E) Controls that visually rely on CSS background-image with no accessible name -> MANUAL_REVIEW
    """
    js = """() => {
      const res = { img_no_alt: [], img_focusable_empty_alt: [], input_image_no_alt: [], graphics_no_name: [], css_bg_controls: [] };
      const q = (sel) => Array.from(document.querySelectorAll(sel));
      const isVisible = (el) => {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return r && r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none';
      };
      const selOf = (el) => {
        const id = el.getAttribute('id'); if (id) return `#${CSS.escape(id)}`;
        let s = el.tagName.toLowerCase();
        if (el.classList.length) s += '.' + Array.from(el.classList).slice(0,3).map(c=>CSS.escape(c)).join('.');
        const name = el.getAttribute('name');
        if (name) s += `[name="${CSS.escape(name)}"]`;
        return s;
      };
      const hasHandlers = (el) => !!(el.getAttribute('onclick') || el.getAttribute('onkeydown') || el.getAttribute('onkeyup'));

      const textOf = (n) => (n && (n.innerText || n.textContent) || "").trim();
      const nearestLabel = (el) => {
        if (el.id) {
          const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (lab && isVisible(lab)) return textOf(lab);
        }
        const wrap = el.closest('label');
        if (wrap && isVisible(wrap)) return textOf(wrap);
        return "";
      };
      const accName = (el) => {
        let name = (el.getAttribute('aria-label') || '').trim();
        if (!name) {
          const ids = (el.getAttribute('aria-labelledby') || "").split(/\\s+/).filter(Boolean);
          if (ids.length) {
            name = ids.map(id => (document.getElementById(id)?.innerText || '')).join(' ').trim();
          }
        }
        if (!name && el.tagName.toLowerCase()==='svg') {
          const t = el.querySelector('title');
          if (t) name = (t.textContent || '').trim();
        }
        if (!name && (el.tagName.toLowerCase()==='img' || el.getAttribute('role')==='img')) {
          name = (el.getAttribute('alt') || '').trim();
        }
        if (!name) name = nearestLabel(el);
        if (!name) name = textOf(el);
        return (name || '').trim();
      };
      const isFocusable = (el) => {
        if (el.tabIndex >= 0) return true;
        const tn = el.tagName.toLowerCase();
        if (tn === 'a' && el.hasAttribute('href')) return true;
        if (tn === 'button' || tn === 'input' || tn === 'select' || tn === 'textarea') return true;
        return false;
      };
      const isDecorativeImg = (img) => {
        const alt = (img.getAttribute('alt') || '').trim();
        const role = (img.getAttribute('role') || '').toLowerCase();
        const ariaHidden = (img.getAttribute('aria-hidden') || '').toLowerCase() === 'true';
        return (alt === '' && !isFocusable(img) && !hasHandlers(img)) || role === 'presentation' || ariaHidden;
      };

      // A) <img> missing alt (not decorative)
      q('img').forEach(img => {
        if (!isVisible(img)) return;
        if (!img.hasAttribute('alt') && !isDecorativeImg(img)) {
          res.img_no_alt.push({ sel: selOf(img) });
        }
      });

      // B) <input type="image"> without alt
      q('input[type="image"]').forEach(el => {
        if (!isVisible(el)) return;
        if (!(el.hasAttribute('alt') && (el.getAttribute('alt')||'').trim())) {
          res.input_image_no_alt.push({ sel: selOf(el) });
        }
      });

      // C) focusable/interactive <img> with empty alt
      q('img').forEach(img => {
        if (!isVisible(img)) return;
        const alt = (img.getAttribute('alt') || '').trim();
        if (alt === '' && (isFocusable(img) || hasHandlers(img))) {
          res.img_focusable_empty_alt.push({ sel: selOf(img) });
        }
      });

      // D) graphics only: <svg> or role="img" with no accessible name
      q('svg, [role="img"]').forEach(el => {
        if (!isVisible(el)) return;
        const ariaHidden = (el.getAttribute('aria-hidden') || '').toLowerCase() === 'true';
        if (ariaHidden) return;
        const name = accName(el);
        if (!name) res.graphics_no_name.push({ sel: selOf(el) });
      });

      // E) Controls that look like purely background-image icons and have no name -> manual review
      q('[style*="background"], [class*="bg"], [class*="hero"], [class*="icon"]').forEach(el => {
        if (!isVisible(el)) return;
        const cs = getComputedStyle(el);
        const bgImg = cs.backgroundImage && cs.backgroundImage !== 'none';
        if (!bgImg) return;
        const role = (el.getAttribute('role') || '').toLowerCase();
        const isInteractive = role === 'button' || role === 'link' || isFocusable(el) || hasHandlers(el) || !!el.closest('a,button');
        if (!isInteractive) return;
        const name = accName(el);
        const ariaHidden = (el.getAttribute('aria-hidden') || '').toLowerCase() === 'true';
        if (!ariaHidden && !name) {
          res.css_bg_controls.push({ sel: selOf(el) });
        }
      });

      return res;
    }"""

    out: List[Dict[str, Any]] = []
    try:
        res = page.evaluate(js)
    except Exception:
        res = {"img_no_alt": [], "img_focusable_empty_alt": [], "input_image_no_alt": [], "graphics_no_name": [], "css_bg_controls": []}

    def _fail(sel: str, note: str) -> Dict[str, Any]:
        cand = _mk_candidate(page, url, "1.1.1", "runner:nontext", sel, note, verdict="fail")
        shot = out_dir / "screenshots" / (sanitize_filename(f"sc111__{(sel or '')[:60]}") + ".png")
        cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=screenshot_elements)
        return cand

    for h in res.get("img_no_alt", []):
        out.append(_fail(h["sel"], "<img> missing alt (not decorative)."))
    for h in res.get("input_image_no_alt", []):
        out.append(_fail(h["sel"], "<input type='image'> missing alt."))
    for h in res.get("img_focusable_empty_alt", []):
        out.append(_fail(h["sel"], "Interactive/focusable <img> has empty alt."))
    for h in res.get("graphics_no_name", []):
        out.append(_fail(h["sel"], "Graphic (<svg>/role=img) lacks accessible name (no <title>/aria-*)."))
    for h in res.get("css_bg_controls", []):
        cand = _mk_candidate(
            page, url, "1.1.1", "runner:css-bg-control", h["sel"],
            "Control appears to rely on CSS background-image; accessible name uncertain.",
            verdict="manual_review"
        )
        shot = out_dir / "screenshots" / (sanitize_filename(f"sc111_bg__{(h['sel'] or '')[:60]}") + ".png")
        cand["screenshot"] = crop_element_screenshot(page, h["sel"], shot, enabled=screenshot_elements)
        out.append(cand)

    return out

def detect_labels_or_instructions_332(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 3.3.2 — Labels or Instructions (mechanical):
      A) Inputs lacking a *programmatic* name (label/aria-label/aria-labelledby) -> FAIL
      B) Radio/checkbox groups lacking a *group label* (fieldset/legend or role='group'/'radiogroup' with name) -> FAIL
      C) Required fields: programmatically required (required/aria-required='true') but *no visible indication* near label -> MANUAL_REVIEW
      D) Complex format fields: pattern/date-like fields with no hint (placeholder/aria-describedby/adjacent help) -> MANUAL_REVIEW

    Notes:
      - Placeholder alone does NOT count as a label (but can be hint for format).
      - We’re conservative: ambiguous cases go to manual_review, not fail.
    """
    js = """() => {
      const q = (sel)=>Array.from(document.querySelectorAll(sel));
      const isVisible = (el) => {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return r && r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none';
      };
      const selOf = (el) => {
        const id = el.getAttribute('id'); if (id) return `#${CSS.escape(id)}`;
        let s = el.tagName.toLowerCase();
        if (el.classList.length) s += '.' + Array.from(el.classList).slice(0,3).map(c=>CSS.escape(c)).join('.');
        const name = el.getAttribute('name');
        if (name) s += `[name="${CSS.escape(name)}"]`;
        return s;
      };
      const textOf = (n) => (n && (n.innerText || n.textContent) || "").trim();
      const nearestLabel = (el) => {
        if (el.id) {
          const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
          if (lab && isVisible(lab)) return textOf(lab);
        }
        const wrap = el.closest('label');
        if (wrap && isVisible(wrap)) return textOf(wrap);
        return "";
      };
      const accName = (el) => {
        let name = el.getAttribute('aria-label') || "";
        if (!name) {
          const ids = (el.getAttribute('aria-labelledby') || "").split(/\\s+/).filter(Boolean);
          if (ids.length) {
            name = ids.map(id => textOf(document.getElementById(id))).join(" ").trim();
          }
        }
        if (!name) {
          const lab = nearestLabel(el);
          if (lab) name = lab;
        }
        return (name || "").trim();
      };
      const describedByText = (el) => {
        const ids = (el.getAttribute('aria-describedby') || "").split(/\\s+/).filter(Boolean);
        let s = ids.map(id => textOf(document.getElementById(id))).join(" ").trim();
        const help = el.closest('.field, .form-group, .input, div') || el.parentElement;
        if (help) {
          const hint = help.querySelector('.help, .hint, .description, small');
          if (hint && isVisible(hint)) s = (s + " " + textOf(hint)).trim();
        }
        return s;
      };
      const hasVisibleRequiredMark = (el) => {
        const lab = nearestLabel(el).toLowerCase();
        if (lab.includes("required")) return true;
        if (/[*]\\s*$/.test(lab) || /[*]\\s*/.test(lab)) return true;
        const root = el.closest('form') || document;
        const note = root.querySelector('p,small,div');
        if (note && /required/.test((note.innerText||"").toLowerCase()) && note.innerText.includes("*")) return true;
        return false;
      };
      const isProbablyComplexFormat = (el) => {
        const type = (el.getAttribute('type') || '').toLowerCase();
        const name = (el.getAttribute('name') || '').toLowerCase();
        const pattern = el.getAttribute('pattern') || '';
        if (pattern) return true;
        if (type && ['date','datetime-local','month','time','week','email','tel','url','number'].includes(type)) return true;
        if (/date|dob|phone|zip|postal|pin|otp/.test(name)) return true;
        return false;
      };
      const getHintText = (el) => {
        const ph = (el.getAttribute('placeholder') || '').trim();
        const desc = describedByText(el);
        return (ph + " " + desc).trim();
      };

      const ctrls = q('input, select, textarea, [role="combobox"], [role="spinbutton"], [role="textbox"]')
        .filter(isVisible);

      const groupRecords = [];
      const groups = new Map();
      ctrls.forEach(el => {
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'radio' || type === 'checkbox') {
          const name = el.getAttribute('name') || selOf(el);
          const field = el.closest('fieldset, [role="group"], [role="radiogroup"]') || el.closest('.form-group, .field, .group') || el.parentElement;
          const key = name + '::' + (field ? selOf(field) : 'root');
          if (!groups.has(key)) groups.set(key, []);
          groups.get(key).push(el);
        }
      });

      const out = { missing_name: [], groups_missing_label: [], required_no_visible_mark: [], complex_without_hint: [] };

      ctrls.forEach(el => {
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'hidden') return;
        const name = accName(el);
        if (!name) {
          out.missing_name.push({ sel: selOf(el) });
        }
      });

      for (const [key, arr] of groups.entries()) {
        if (arr.length < 2) continue;
        const container = arr[0].closest('fieldset, [role="group"], [role="radiogroup"]');
        let hasGroupLabel = false;
        if (container) {
          const lg = container.querySelector('legend');
          if (lg && isVisible(lg) && textOf(lg)) hasGroupLabel = true;
          const r = (container.getAttribute('role') || '').toLowerCase();
          if (!hasGroupLabel && (r === 'group' || r === 'radiogroup')) {
            const acc = container.getAttribute('aria-label') || '';
            if (acc.trim()) hasGroupLabel = true;
            const ids = (container.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
            if (!hasGroupLabel && ids.length) {
              const nm = ids.map(id => (document.getElementById(id)?.innerText || "")).join(" ").trim();
              if (nm) hasGroupLabel = true;
            }
          }
        }
        if (!hasGroupLabel) {
          out.groups_missing_label.push({ sel: selOf(arr[0]) });
        }
      }

      ctrls.forEach(el => {
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'hidden') return;
        const required = el.hasAttribute('required') || (el.getAttribute('aria-required') || '').toLowerCase() === 'true';
        if (!required) return;
        if (!hasVisibleRequiredMark(el)) {
          out.required_no_visible_mark.push({ sel: selOf(el) });
        }
      });

      ctrls.forEach(el => {
        if (!isProbablyComplexFormat(el)) return;
        const hint = getHintText(el).toLowerCase();
        if (!hint || hint.length < 2) {
          out.complex_without_hint.push({ sel: selOf(el) });
        }
      });

      return out;
    }"""

    out: List[Dict[str, Any]] = []
    try:
        res = page.evaluate(js)
    except Exception:
        res = {"missing_name": [], "groups_missing_label": [], "required_no_visible_mark": [], "complex_without_hint": []}

    def _fail(sel: str, note: str) -> Dict[str, Any]:
        cand = _mk_candidate(page, url, "3.3.2", "runner:labels-or-instructions", sel, note, verdict="fail")
        shot = out_dir / "screenshots" / (sanitize_filename(f"sc332__{(sel or '')[:60]}") + ".png")
        cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=screenshot_elements)
        return cand

    def _review(sel: str, note: str) -> Dict[str, Any]:
        cand = _mk_candidate(page, url, "3.3.2", "runner:labels-or-instructions", sel, note, verdict="manual_review")
        shot = out_dir / "screenshots" / (sanitize_filename(f"sc332_rev__{(sel or '')[:60]}") + ".png")
        cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=screenshot_elements)
        return cand

    for h in res.get("missing_name", []):
        out.append(_fail(h["sel"], "Form control lacks a programmatic label (label/aria-label/aria-labelledby)."))

    for h in res.get("groups_missing_label", []):
        out.append(_fail(h["sel"], "Radio/checkbox group missing group label (fieldset/legend or named group)."))

    for h in res.get("required_no_visible_mark", []):
        out.append(_review(h["sel"], "Required field without a visible required indication near its label (verify with design guidelines)."))

    for h in res.get("complex_without_hint", []):
        out.append(_review(h["sel"], "Complex input format without visible hint/example (e.g., MM/DD/YYYY, phone format)."))

    return out

def detect_contrast_text_general(page, url: str, out_dir: pathlib.Path) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    """
    SC 1.4.3 — text contrast on solid backgrounds (non-UI text).
    - If element appears to be a control (button/link/form role), send MANUAL_REVIEW instead (we rely on 1.4.11 for UI chrome).
    - Pseudo/gradient backgrounds -> manual_review.
    """
    js = """() => {
      const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,li,dt,dd,th,td,small,em,strong'));
      const res = [];
      for (const el of all) {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        const txt = (el.innerText || '').trim();
        if (!txt || !(r.width>40 && r.height>14)) continue;

        const tn = el.tagName.toLowerCase();
        const isLink = el.closest('a[href]') && tn !== 'a' ? true : (tn === 'a' && el.hasAttribute('href'));
        const isControl = isLink || el.closest('button,input,select,textarea,[role="button"],[role="link"],[role="tab"],[role="switch"],[role="checkbox"],[role="radio"],[role="combobox"],[role="listbox"]');

        const bgImg = cs.backgroundImage && cs.backgroundImage !== 'none';
        const hasBefore = window.getComputedStyle(el, '::before').content && window.getComputedStyle(el, '::before').content !== 'none';
        const hasAfter  = window.getComputedStyle(el, '::after').content && window.getComputedStyle(el, '::after').content !== 'none';
        let bg_source = "solid";
        if (bgImg) bg_source = "gradient";
        if (hasBefore || hasAfter) bg_source = "pseudo";

        res.push({
          sel: (window.__a11ySel? window.__a11ySel(el) : ''),
          role: el.getAttribute('role') || tn,
          color: cs.color,
          fontSize: cs.fontSize,
          bg: cs.backgroundColor,
          rect:{x:r.x,y:r.y,w:r.width,h:r.height},
          bg_source,
          isControl: !!isControl
        });
      }
      return res.slice(0, 160);
    }"""
    fails, passes = [], []
    try:
        elems = page.evaluate(js)
        if not elems:
            return fails, passes
        full_path = out_dir / "screenshots" / "contrast_text_full.png"
        ensure_dir(full_path.parent)
        page.screenshot(path=str(full_path), full_page=True)
        im = Image.open(full_path)

        for e in elems:
            sel = e["sel"]; rect = e["rect"]; role = e.get("role") or ""
            if e.get("isControl"):
                cand = _mk_candidate(
                    page, url, "1.4.3", "runner:contrast-text", sel,
                    "Text belongs to a UI control — evaluate under 1.4.11 (non-text contrast) or review.",
                    verdict="manual_review", evidence={"context":"ui_control"}
                )
                fails.append(cand)
                continue

            bg_source = e.get("bg_source") or "solid"
            if bg_source in ("pseudo","gradient","unresolved"):
                cand = _mk_candidate(
                    page, url, "1.4.3", "runner:contrast-text", sel,
                    "Undetermined background (pseudo/gradient) — manual verification required.",
                    verdict="manual_review", evidence={"bg_source": bg_source}
                )
                fails.append(cand)
                continue

            fr, fg, fb, fa = _parse_rgba_any(e["color"])
            if fa == 0:
                continue
            fg_rgb = (fr, fg, fb)
            bg_css = e.get("bg","")
            br, bg_g, bb, ba = _parse_rgba_any(bg_css)
            if ba == 0 or bg_css in ("transparent","rgba(0, 0, 0, 0)","rgba(0,0,0,0)"):
                bg_rgb = _sample_bg_from_image(im, rect)
            else:
                bg_rgb = (br, bg_g, bb)

            ratio = _contrast_ratio(fg_rgb, bg_rgb)
            fs_px = float(re.findall(r"[\d.]+", e.get("fontSize","16px"))[0])
            try:
                fw = page.eval_on_selector(sel, "(el)=>getComputedStyle(el).fontWeight") if sel else "400"
                is_bold = int(fw) >= 700
            except Exception:
                is_bold = False
            is_large = (fs_px >= 24) or (is_bold and fs_px >= 18.66)
            pass_thr = (CONTRAST_MIN_LARGE if is_large else CONTRAST_MIN_NORMAL)
            evidence = {"contrast_ratio": round(ratio,2), "is_large_text": is_large, "role": role}
            note = f"Text contrast {ratio:.2f} {'<' if ratio<pass_thr else '>='} threshold {pass_thr:.1f}."
            if ratio < pass_thr:
                cand = _mk_candidate(page, url, "1.4.3", "runner:contrast-text", sel, note, verdict="fail", evidence=evidence)
                shot = out_dir / "screenshots" / (sanitize_filename(f"contrast_text__{(sel or '')[:60]}") + ".png")
                cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=True)
                fails.append(cand)
            else:
                passes.append({"selector": sel, "sc":"1.4.3", "note": note, "evidence": evidence})
    except Exception:
        pass
    return fails, passes

def detect_contrast_on_image_text(page, url: str, out_dir: pathlib.Path) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    """
    SC 1.4.3 — text over images/gradients.
    We do not trust automatic background resolution → manual_review with a crop for quick human check.
    """
    js = """() => {
      const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,p,span,a,button'));
      const res = [];
      for (const el of all) {
        const cs = getComputedStyle(el);
        const txt = (el.innerText || '').trim();
        if (!txt) continue;
        const rect = el.getBoundingClientRect();
        if (!(rect.width > 40 && rect.height > 16)) continue;
        const bgImg = cs.backgroundImage && cs.backgroundImage !== 'none';
        const hasAncestorBg = !!el.closest('[style*="background"], .bg, .hero, .banner');
        if (!bgImg && !hasAncestorBg) continue;
        res.push({ sel: (window.__a11ySel? window.__a11ySel(el) : ''), rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}, bg_source: (bgImg ? "gradient" : "pseudo") });
      }
      return res.slice(0, 40);
    }"""
    fails, passes = [], []
    try:
        elems = page.evaluate(js)
        if not elems:
            return fails, passes
        full_path = out_dir / "screenshots" / "contrast_over_image_full.png"
        ensure_dir(full_path.parent)
        page.screenshot(path=str(full_path), full_page=True)
        im = Image.open(full_path)

        for e in elems:
            sel = e["sel"]; rect = e["rect"]; bg_source = e.get("bg_source") or "pseudo"
            cand = _mk_candidate(
                page, url, "1.4.3", "runner:contrast-over-image", sel,
                "Text over image/gradient — send to Manual Review.", verdict="manual_review",
                evidence={"bg_source": bg_source}
            )
            shot = out_dir / "screenshots" / (sanitize_filename(f"contrast_over_img__{(sel or '')[:60]}") + ".png")
            try:
                box = (int(rect["x"]), int(rect["y"]), int(rect["x"]+rect["w"]), int(rect["y"]+rect["h"]))
                ensure_dir(shot.parent)
                im.crop(box).save(shot)
                cand["screenshot"] = str(shot)
            except Exception:
                pass
            fails.append(cand)
    except Exception:
        pass
    return fails, passes

def detect_contrast_nontext_ui(page, url: str, out_dir: pathlib.Path) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    js = """() => {
      const res = [];
      const isUI = (el) => el.matches('button, input, select, textarea, [role="button"], [role="switch"], [role="checkbox"], [role="radio"], [role="tab"], [role="link"]');
      const nodes = Array.from(document.querySelectorAll('button, input, select, textarea, [role]'));
      for (const el of nodes) {
        if (!isUI(el)) continue;
        const rect = el.getBoundingClientRect();
        if (!(rect.width>20 && rect.height>16)) continue;
        const cs = getComputedStyle(el);
        const bgImg = cs.backgroundImage && cs.backgroundImage !== 'none';
        const beforeC = window.getComputedStyle(el, '::before').content;
        const afterC  = window.getComputedStyle(el, '::after').content;
        const hasPseudo = (beforeC && beforeC !== 'none') || (afterC && afterC !== 'none');
        let bg_source = "solid";
        if (bgImg) bg_source = "gradient";
        if (hasPseudo) bg_source = "pseudo";
        res.push({
          sel: (window.__a11ySel? window.__a11ySel(el) : ''),
          rect:{x:rect.x,y:rect.y,w:rect.width,h:rect.height},
          bg_source
        });
      }
      return res.slice(0, 120);
    }"""
    fails, passes = [], []
    try:
        elems = page.evaluate(js)
        if not elems:
            return fails, passes
        full_path = out_dir / "screenshots" / "contrast_nontext_full.png"
        ensure_dir(full_path.parent)
        page.screenshot(path=str(full_path), full_page=True)
        im = Image.open(full_path)

        for e in elems:
            sel = e["sel"]; rect = e["rect"]
            bg_source = e.get("bg_source") or "solid"
            if bg_source in ("pseudo","gradient","unresolved"):
                cand = _mk_candidate(
                    page, url, "1.4.11", "runner:nontext-ui-contrast",
                    sel, "Undetermined component background (pseudo/gradient) — Manual Review.",
                    verdict="manual_review", evidence={"bg_source": bg_source}
                )
                fails.append(cand)
                continue

            inside_rgb = _sample_bg_from_image(im, rect)
            ring_rect = {"x": max(0, rect["x"] - 4), "y": max(0, rect["y"] - 4), "w": rect["w"] + 8, "h": rect["h"] + 8}
            outside_rgb = _sample_bg_from_image(im, ring_rect)
            ratio = _contrast_ratio(inside_rgb, outside_rgb)
            evidence = {"contrast_ratio": round(ratio,2), "inside_rgb": inside_rgb, "outside_rgb": outside_rgb}
            if ratio < CONTRAST_NON_TEXT_UI:
                cand = _mk_candidate(
                    page, url, "1.4.11", "runner:nontext-ui-contrast", sel,
                    f"Non-text contrast {ratio:.2f} < 3.0", verdict="fail", evidence=evidence
                )
                shot = out_dir / "screenshots" / (sanitize_filename(f"contrast_nontext__{(sel or '')[:60]}") + ".png")
                cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=True)
                fails.append(cand)
            else:
                passes.append({"selector": sel, "sc":"1.4.11", "note": "Non-text contrast OK", "evidence": evidence})
    except Exception:
        pass
    return fails, passes

# -------------------- other detectors --------------------

def detect_info_relationships(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 1.3.1 — In header/footer/nav regions with multiple links, links should be grouped with list semantics.
    - Only evaluate <a href> links
    - Do NOT flag buttons/CTAs
    - Require at least 3 links in the region (avoid noisy singles)
    """
    js = """() => {
      const out = [];
      const regions = Array.from(document.querySelectorAll('header, footer, nav, [role="navigation"]'));
      const selOf = (el) => {
        const id = el.getAttribute('id'); if (id) return `#${CSS.escape(id)}`;
        let s = el.tagName.toLowerCase();
        if (el.classList.length) s += '.' + Array.from(el.classList).slice(0,3).map(c=>CSS.escape(c)).join('.');
        return s;
      };
      const isWrappedInList = (el, root) => {
        let p = el;
        while (p && p !== root) {
          const tn = (p.tagName || '').toLowerCase();
          if (tn === 'ul' || tn === 'ol' || tn === 'li' || p.getAttribute('role')==='list' || p.getAttribute('role')==='listitem') return true;
          p = p.parentElement;
        }
        return false;
      };
      for (const reg of regions) {
        const links = Array.from(reg.querySelectorAll('a[href]')).filter(a => {
          const r = a.getBoundingClientRect(); const cs = getComputedStyle(a);
          return r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none';
        });
        if (links.length < 3) continue;
        for (const a of links) {
          if (!isWrappedInList(a, reg)) {
            out.push({ sel: selOf(a) });
          }
        }
      }
      return out.slice(0, 200);
    }"""
    out = []
    try:
        hits = page.evaluate(js)
        for h in hits:
            sel = h.get("sel") or ""
            cand = _mk_candidate(
                page, url, "1.3.1", "runner:info-relationships-list-missing", sel,
                "Navigation links not grouped using list semantics.", verdict="fail"
            )
            shot_path = (out_dir / "screenshots" / (sanitize_filename(f"rel_nav__{(sel or '')[:60]}") + ".png"))
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
    except Exception:
        pass
    return out

def detect_heading_outline_sequence(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str,Any]]:
    """
    SC 1.3.2 (assist) — Check heading level jumps (e.g., H4 after H2 with no H3 in between).
    Conservative: produce MANUAL_REVIEW, not FAIL.
    """
    js = """() => {
      const heads = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]'));
      const items = heads.map(h => {
        const lvl = h.tagName.match(/^H([1-6])$/) ? +RegExp.$1 : +(h.getAttribute('aria-level') || 0);
        const r = h.getBoundingClientRect();
        return { el: h, level: lvl || 0, text: (h.innerText||'').trim(), y: r.top };
      }).filter(x => x.level>0 && x.text);
      const out = [];
      for (let i=1;i<items.length;i++){
        const prev = items[i-1], cur = items[i];
        if (cur.level - prev.level > 1) {
          out.push({ sel: null, idx: i });
        }
      }
      return out;
    }"""
    out = []
    try:
        jumps = page.evaluate(js)
        if not jumps:
            return out
        for j in jumps[:12]:
            note = "Possible heading level jump in outline (e.g., H2 → H4). Verify reading order."
            cand = _mk_candidate(page, url, "1.3.2", "runner:heading-outline-jump", "", note, verdict="manual_review")
            out.append(cand)
    except Exception:
        pass
    return out

def detect_role_conflicts(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 4.1.2 — role conflicts / nested interactive (unchanged).
    """
    js = """() => {
      const out = [];
      const all = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"]'));
      for (const el of all) {
        const tn = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || '';
        const hasNestedInteractive = !!el.querySelector('a, button, [role="button"], [role="link"]');
        if ((tn === 'a' && role === 'button') || hasNestedInteractive) {
          out.push({ sel: (window.__a11ySel? window.__a11ySel(el): '') });
        }
      }
      return out.slice(0, 20);
    }"""
    out = []
    try:
        hits = page.evaluate(js)
        for h in hits:
            sel = h["sel"]
            cand = _mk_candidate(
                page, url, "4.1.2", "runner:role-conflict-or-nested", sel,
                "Potential role conflict or nested interactive elements.", verdict="fail"
            )
            shot_name = sanitize_filename(f"roleconf__{(sel or '')[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
    except Exception:
        pass
    return out

def _normalize_text_label(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return s.strip()

def _accname_for_selector(page, sel: str) -> str:
    js = """(el) => {
      if(!el) return "";
      const aria = el.getAttribute('aria-label');
      if (aria && aria.trim()) return aria.trim();
      const ids = (el.getAttribute('aria-labelledby')||"").trim().split(/\\s+/).filter(Boolean);
      let by = "";
      for (const id of ids) {
        const t = document.getElementById(id);
        if (t && (t.innerText || t.textContent)) {
          by += " " + (t.innerText || t.textContent);
        }
      }
      by = by.trim();
      if (by) return by;
      const txt = (el.innerText || el.textContent || "").trim();
      return txt;
    }"""
    try:
        return (page.eval_on_selector(sel, js) or "").strip()
    except Exception:
        return ""

def detect_label_in_name(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 2.5.3 — Visible label included in accessible name.
    - Token containment (case/spacing/punct insensitive)
    - Re-check aria-labelledby resolution
    - Ignore ultra-short tokens (<=2 chars) and purely icon buttons
    """
    STOP = {"the","and","to","for","of","in","on","at","a","an","or"}
    def toks(s: str) -> List[str]:
        s = _normalize_text_label(s)
        return [t for t in s.split() if len(t) > 2 and t not in STOP]

    js = """() => {
      const els = Array.from(document.querySelectorAll('button, a[href], input, [role="button"], [role="link"]'));
      const res = [];
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (!(r.width>24 && r.height>16)) continue;
        let vis = '';
        if (el.id) {
          const lab = document.querySelector(`label[for="${el.id}"]`);
          if (lab) vis = (lab.innerText || '').trim();
        }
        if (!vis) vis = (el.innerText || el.getAttribute('aria-label') || '').trim();
        if (!vis) continue;
        res.push({ sel: (window.__a11ySel? window.__a11ySel(el): ''), visible: vis.slice(0,160), labelledby: el.getAttribute('aria-labelledby') || '' });
      }
      return res.slice(0, 120);
    }"""
    out = []
    try:
        rows = page.evaluate(js)
        for r in rows:
            sel = r["sel"]
            vis_tokens = toks(r.get("visible") or "")
            if not vis_tokens:
                continue
            acc = _normalize_text_label(_accname_for_selector(page, sel))
            missing = [t for t in vis_tokens if t not in acc.split()]
            if missing:
                if r.get("labelledby"):
                    acc2 = _normalize_text_label(_accname_for_selector(page, sel))
                    if all(t in acc2.split() for t in vis_tokens):
                        continue
                cand = _mk_candidate(
                    page, url, "2.5.3", "runner:label-in-name", sel,
                    "Visible label text not included in the accessible name.", verdict="fail"
                )
                shot_name = sanitize_filename(f"lin__{(sel or '')[:60]}") + ".png"
                shot_path = (out_dir / "screenshots" / shot_name)
                cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                out.append(cand)
    except Exception:
        pass
    return out

def detect_link_purpose_generic(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 2.4.4 — generic link text; allow nearby heading to disambiguate.
    """
    GENERIC = {"learn more","get started","read more","see more","view more","more","details","click here"}

    def _clean_link_text(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"[\u2190-\u21ff\u25a0-\u25ff\u2700-\u27bf➡️•►»›‹«→]+", "", s)
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"[^a-z ]", "", s)
        return s.strip()

    def _nearby_heading_text(page, sel: str) -> str:
        js = """(el) => {
          if (!el) return "";
          const hs = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]'));
          let best = "", bestDist = Infinity;
          const r = el.getBoundingClientRect();
          for (const h of hs) {
            const rb = h.getBoundingClientRect();
            if (rb.top <= r.top + 2) {
              const d = Math.abs(r.top - rb.top) + Math.abs(r.left - rb.left);
              if (d < bestDist) {
                bestDist = d;
                best = (h.innerText || h.getAttribute('aria-label') || '').trim();
              }
            }
          }
          return best;
        }"""
        try:
            t = page.eval_on_selector(sel, js) or ""
            return re.sub(r"\s+", " ", t).strip().lower()
        except Exception:
            return ""

    js = """() => {
      const res = [];
      const links = Array.from(document.querySelectorAll('a[href]'));
      for (const a of links) {
        const txt = (a.innerText || '').trim().toLowerCase();
        const rect = a.getBoundingClientRect();
        if (!(rect.width>16 && rect.height>12)) continue;
        res.push({ sel: (window.__a11ySel? window.__a11ySel(a): ''), text: txt });
      }
      return res.slice(0, 400);
    }"""
    out = []
    try:
        rows = page.evaluate(js)
        for r in rows:
            text = _clean_link_text(r["text"])
            if text in GENERIC:
                sel = r["sel"]
                ctx = _nearby_heading_text(page, sel)
                if ctx:
                    continue
                cand = _mk_candidate(
                    page, url, "2.4.4", "runner:link-purpose-generic", sel,
                    f'Generic link text: "{text}" without disambiguating context.', verdict="fail"
                )
                shot_name = sanitize_filename(f"lp__{(sel or '')[:60]}") + ".png"
                shot_path = (out_dir / "screenshots" / shot_name)
                cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
                out.append(cand)
    except Exception:
        pass
    return out

def detect_link_indicator_style(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    """
    SC 1.4.1 — inline links must be visually distinguishable (unchanged).
    """
    js = """() => {
      const out = [];
      const links = Array.from(document.querySelectorAll('a[href]'));
      for (const a of links) {
        const rect = a.getBoundingClientRect();
        if (!(rect.width>16 && rect.height>12)) continue;
        if (a.querySelector('img')) continue;
        const cs = getComputedStyle(a);
        const p = a.parentElement || document.body;
        const cps = getComputedStyle(p);
        const underline = (cs.textDecorationLine || '').includes('underline');
        const colorDiff = cs.color !== cps.color;
        if (!underline && !colorDiff) {
          out.push({ sel: (window.__a11ySel? window.__a11ySel(a): '') });
        }
      }
      return out.slice(0, 40);
    }"""
    out = []
    try:
        rows = page.evaluate(js)
        for r in rows:
            sel = r["sel"]
            cand = _mk_candidate(
                page, url, "1.4.1", "link-indicator:style", sel,
                "Inline link not visually distinguished (no underline and no color difference).", verdict="fail"
            )
            shot_name = sanitize_filename(f"linkind__{(sel or '')[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
    except Exception:
        pass
    return out

# -------------------- timeouts (unchanged) --------------------

TIMEOUT_20H_SECONDS = 20 * 60 * 60
TIMEOUT_20H_MS = TIMEOUT_20H_SECONDS * 1000

def detect_timeouts(page, url: str, out_dir: pathlib.Path) -> List[Dict[str, Any]]:
    js = """() => {
      const metas = [];
      document.querySelectorAll('meta[http-equiv]').forEach(m => {
        const hev = (m.getAttribute('http-equiv') || m.getAttribute('httpEquiv') || '').toLowerCase();
        if (hev === 'refresh') {
          const content = m.getAttribute('content') || '';
          metas.push({ type:'meta', content });
        }
      });
      const inlineScripts = Array.from(document.querySelectorAll('script:not([src])'))
        .slice(0,80)
        .map(s => s.textContent || '')
        .join('\\n');
      return { metas, inlineScripts: inlineScripts.slice(0, 200000) };
    }"""
    out = []
    try:
        res = page.evaluate(js)
        for m in res.get("metas", []):
            c = m.get("content","")
            secs = None
            try:
                secs = int((c.split(";")[0] or "").strip())
            except Exception:
                secs = None
            if secs is not None and secs < TIMEOUT_20H_SECONDS:
                cand = _mk_candidate(
                    page, url, "2.2.6", "timeout:meta-refresh", "",
                    f"Meta refresh present (~{secs}s) without user control/warning.", verdict="fail"
                )
                out.append(cand)
        code = res.get("inlineScripts","") or ""
        for m in re.finditer(r"setTimeout\s*\(([^,]+),\s*(\d+)\s*\)", code, flags=re.S|re.I):
            delay_ms = int(m.group(2))
            callback_src = m.group(1)
            if delay_ms < TIMEOUT_20H_MS and re.search(r"location|redirect|logout|session", callback_src, flags=re.I):
                cand = _mk_candidate(
                    page, url, "2.2.6", "timeout:settimeout", "",
                    f"Script sets a time-based action at ~{int(delay_ms/1000)}s (possible timeout) without visible notice.",
                    verdict="fail"
                )
                out.append(cand)
    except Exception:
        pass
    return out

# -------------------- focus-visible / focus-order from keyboard trace (unchanged) --------------------

def _read_keyboard_trace(out_dir: pathlib.Path) -> List[Dict[str, Any]]:
    rows = []
    p = out_dir / "keyboard_trace.jsonl"
    if not p.exists():
        return rows
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows

def detect_focus_visible_weak_from_trace(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    trace = _read_keyboard_trace(out_dir)
    out = []
    seen = set()
    for t in trace:
        sel = t.get("selector") or ""
        if not sel or sel in seen:
            continue
        outline = (t.get("outline") or "").lower()
        boxshadow = (t.get("boxShadow") or "").lower()
        border = (t.get("border") or "").lower()
        weak = (("none" in outline or outline.strip() == "")) and (boxshadow.strip() == "") and (("0px" in border) or ("none" in border))
        if weak:
            cand = _mk_candidate(page, url, "2.4.7", "focus-visible:weak", sel, "Focused element may lack a visible focus indicator.", verdict="fail")
            shot_name = sanitize_filename(f"fvis__{sel[:60]}") + ".png"
            shot_path = (out_dir / "screenshots" / shot_name)
            cand["screenshot"] = crop_element_screenshot(page, sel, shot_path, enabled=screenshot_elements)
            out.append(cand)
            seen.add(sel)
            if len(out) >= 20:
                break
    return out

def detect_focus_order_suspect_from_trace(page, url: str, out_dir: pathlib.Path, screenshot_elements: bool) -> List[Dict[str, Any]]:
    trace = _read_keyboard_trace(out_dir)
    out = []
    for i in range(1, len(trace)):
        prev = trace[i-1]; cur = trace[i]
        rp = (prev.get("rect") or {}); rc = (cur.get("rect") or {})
        if not rp or not rc:
            continue
        dy = rc.get("y", 0) - rp.get("y", 0)
        if dy < -400:
            sel = cur.get("selector") or ""
            cand = _mk_candidate(
                page, url, "2.4.3", "runner:focus-order-suspect",
                sel, "Focus order may not follow a meaningful sequence (large jump detected).",
                verdict="manual_review"
            )
            shot = out_dir / "screenshots" / (sanitize_filename(f"forder__{sel[:60]}") + ".png")
            cand["screenshot"] = crop_element_screenshot(page, sel, shot, enabled=screenshot_elements)
            out.append(cand)
            if len(out) >= 10:
                break
    return out
