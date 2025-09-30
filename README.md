# A11y E2E App — Streamlit UI + axe-core baseline + RAG semantic reviewer + Excel/Word reports

## What this is
An end-to-end accessibility auditing skeleton that:
- Runs **axe-core** via Playwright to generate deterministic issues,
- Routes candidates to semantic topics and builds **RAG prompts**,
- (Optional) Calls your LLM for semantic judgments,
- Generates **Excel** and **Word** reports aligned with your sample layout,
- Provides a **Streamlit** UI to run single-URL audits and download reports.

## Quickstart
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install --with-deps   # Windows: python -m playwright install
streamlit run app/streamlit_app.py
```

Then paste a URL and click **Run Audit**. Outputs go under `out/<slug>/`:
- `axe_results.json`, `candidates.json`, `ai_verdicts.json`
- `report.xlsx`, `report.docx`
- `screenshots/` crops for evidence

## Configure LLM (optional)
Set `OPENAI_API_KEY` and `OPENAI_MODEL` in your environment. The default reviewer is stubbed to return demo verdicts; wire your provider in `core/rag_reviewer.py`.
