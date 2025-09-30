# --- make project root importable (must be first) ---
import sys, os, asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ----------------------------------------------------

# --- Windows: ensure asyncio can spawn subprocesses for Playwright ---
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# --------------------------------------------------------------------

import json
from urllib.parse import urlparse
import streamlit as st

from core.axe_runner import run_axe_on_url
from core.rag_reviewer import review as rag_review
from core.report_builders.excel_report import build_excel
from core.report_builders.word_report import build_word

st.set_page_config(page_title="A11y Audit — axe + RAG", layout="wide")

# ======================= secrets → env =======================
def _apply_secrets_to_env():
    """
    Read Streamlit secrets and expose as environment variables so core/*
    modules can use them without importing Streamlit.
    """
    flat = st.secrets
    nested = flat.get("openai", {}) if hasattr(st, "secrets") else {}

    def set_if_missing(name, value, default=None):
        if os.environ.get(name):
            return
        if value is not None and str(value).strip():
            os.environ[name] = str(value)
        elif default is not None:
            os.environ[name] = str(default)

    # creds + model
    set_if_missing("OPENAI_API_KEY", flat.get("OPENAI_API_KEY") or nested.get("api_key"))
    set_if_missing("OPENAI_MODEL",   flat.get("OPENAI_MODEL")   or nested.get("model"), "gpt-4o-mini")

    # feature flags
    # default ON if a key is present; otherwise force OFF
    if os.environ.get("OPENAI_API_KEY"):
        set_if_missing("A11Y_USE_LLM", flat.get("A11Y_USE_LLM"), "1")
    else:
        os.environ["A11Y_USE_LLM"] = "0"

    # optional knob: skip best-practice (non-WCAG-tagged) items
    set_if_missing("A11Y_SKIP_BEST_PRACTICE", flat.get("A11Y_SKIP_BEST_PRACTICE"), "0")

_apply_secrets_to_env()

# ======================= helpers =======================
BASE_OUT = (ROOT / "out")
BASE_OUT.mkdir(exist_ok=True)

def slugify(u: str) -> str:
    p = urlparse(u)
    host = (p.netloc or "site").replace(":", "_")
    path = (p.path or "/").strip("/").replace("/", "_") or "home"
    return f"{host}__{path}"[:80]

@st.cache_data(show_spinner=False)
def zip_run_dir(run_dir_str: str) -> bytes:
    """Zip an output directory into a single bytes object (cached)."""
    import io, zipfile
    run_dir = Path(run_dir_str)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(run_dir):
            for f in files:
                p = Path(root) / f
                arcname = Path(run_dir.name) / p.relative_to(run_dir)
                z.write(p, arcname.as_posix())
    buf.seek(0)
    return buf.getvalue()

def show_preview_tables(out_dir: Path):
    try:
        cands = json.loads((out_dir / "candidates.json").read_text(encoding="utf-8"))
        ai = json.loads((out_dir / "ai_verdicts.json").read_text(encoding="utf-8"))
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Candidates")
            st.dataframe(cands, use_container_width=True)
        with col2:
            st.subheader("AI Verdicts")
            st.dataframe(ai, use_container_width=True)
    except Exception as e:
        st.info(f"Preview not available yet: {e}")

def build_reports(out_dir: Path, url: str):
    xlsx = out_dir / "report.xlsx"
    docx = out_dir / "report.docx"
    build_excel(out_dir, xlsx)
    build_word(out_dir, docx, url)
    return xlsx, docx

# ======================= UI =======================
st.title("Accessibility Audit (axe-core + RAG)")

# ---- Sidebar controls ----
with st.sidebar:
    st.header("AI Settings")

    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    # Simple toggle; disabled if no key present in secrets/env
    use_live_ai = st.checkbox(
        "Use live AI",
        value=(os.environ.get("A11Y_USE_LLM") == "1" and has_key),
        disabled=not has_key,
        help="Runs the semantic reviewer with your OpenAI key from Streamlit Secrets."
    )
    os.environ["A11Y_USE_LLM"] = "1" if (use_live_ai and has_key) else "0"

    skip_bp = st.checkbox(
        "Skip non-WCAG (best-practice) rules in AI",
        value=(os.environ.get("A11Y_SKIP_BEST_PRACTICE") == "1"),
        help="If ON, AI runs only for candidates with WCAG SC tags (reduces noise)."
    )
    os.environ["A11Y_SKIP_BEST_PRACTICE"] = "1" if skip_bp else "0"

    st.caption(f"Model: {os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')}")
    st.caption(f"AI reviewer: {'ON' if os.environ.get('A11Y_USE_LLM')=='1' and has_key else 'OFF'}")

    st.divider()
    st.header("Import WCAG Techniques")
    xls = st.file_uploader("Upload WCAG_Checklist.xlsx", type=["xlsx", "xls"])
    merge_existing = st.checkbox(
        "Merge with existing JSONs (append/unique)",
        value=True,
        help="If off, existing files will be overwritten."
    )
    if xls and st.button("Import techniques"):
        from core.wcag_importer import import_wcag_from_excel, WCAG_LIB_DIR
        result = import_wcag_from_excel(xls.getvalue(), out_dir=WCAG_LIB_DIR, merge_existing=merge_existing)
        st.success(f"Imported: {result['created']} created, {result['updated']} updated, {result['skipped']} skipped")
        for p in result["files"][:10]:
            st.write("•", p)
        if len(result["files"]) > 10:
            st.write(f"... and {len(result['files']) - 10} more")

    # Tiny status of loaded technique files
    try:
        wcag_dir = ROOT / "core" / "wcag_lib"
        files = list(wcag_dir.glob("sc-*.json"))
        st.caption(f"WCAG techniques loaded: {len(files)}")
        if not files:
            st.warning("No technique JSONs found. Using minimal fallback context.")
    except Exception:
        pass

# ---- Main controls ----
if "last_url" not in st.session_state:
    st.session_state.last_url = "https://example.com"
if "last_run" not in st.session_state:
    st.session_state.last_run = None  # dict with url/slug/paths

url = st.text_input("Enter a URL to audit", value=st.session_state.last_url, help="Single page for now. We’ll add crawling later.")
colA, colB, colC = st.columns([1, 1, 1])
run_clicked  = colA.button("Run Full Audit", type="primary", key="run_audit")
reai_clicked = colB.button("Rebuild AI Verdicts Only", key="reai")  # doesn’t rerun axe
clear_clicked = colC.button("Clear Session", key="clear")

if clear_clicked:
    st.session_state.last_run = None
    st.session_state.last_url = url
    st.cache_data.clear()
    st.success("Session cleared.")

# ======================= actions =======================
if run_clicked and url:
    st.session_state.last_url = url
    slug = slugify(url)
    out_dir = BASE_OUT / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    with st.spinner("Running axe-core…"):
        run_axe_on_url(url, out_dir)

    with st.spinner("Building RAG prompts & AI verdicts…"):
        rag_review(out_dir)  # live or stub based on env A11Y_USE_LLM

    with st.spinner("Generating Excel & Word reports…"):
        xlsx, docx = build_reports(out_dir, url)

    st.session_state.last_run = {
        "url": url,
        "slug": slug,
        "out_dir": str(out_dir),
        "xlsx": str(xlsx),
        "docx": str(docx),
    }
    st.success("Audit completed.")

elif reai_clicked and st.session_state.last_run:
    # Re-run only the AI step on existing candidates (no Playwright)
    lr = st.session_state.last_run
    out_dir = Path(lr["out_dir"])
    with st.spinner("Rebuilding RAG prompts & AI verdicts…"):
        rag_review(out_dir)
    with st.spinner("Regenerating Excel & Word reports…"):
        xlsx, docx = build_reports(out_dir, lr["url"])
    st.session_state.last_run["xlsx"] = str(xlsx)
    st.session_state.last_run["docx"] = str(docx)
    st.success("AI verdicts and reports updated.")

# ======================= downloads & preview =======================
if st.session_state.last_run:
    lr = st.session_state.last_run
    out_dir = Path(lr["out_dir"])
    slug = lr["slug"]
    xlsx = Path(lr["xlsx"])
    docx = Path(lr["docx"])

    st.divider()
    st.subheader("Downloads")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("Download Excel report", data=xlsx.read_bytes(), file_name=xlsx.name, key="dl_excel")
    with c2:
        st.download_button("Download Word report", data=docx.read_bytes(), file_name=docx.name, key="dl_word")
    with c3:
        zip_bytes = zip_run_dir(str(out_dir))
        st.download_button(
            "Download ALL outputs (ZIP)",
            data=zip_bytes,
            file_name=f"{slug}.zip",
            mime="application/zip",
            key="dl_zip_all"
        )

    st.divider()
    st.subheader("Run Artifacts")
    st.code(json.dumps({
        "axe_results": str(out_dir / "axe_results.json"),
        "candidates": str(out_dir / "candidates.json"),
        "ai_verdicts": str(out_dir / "ai_verdicts.json"),
        "screenshots_dir": str(out_dir / "screenshots")
    }, indent=2))

    show_preview_tables(out_dir)
