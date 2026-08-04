"""
dwg_pipeline — odysseus integration layer for the DWG → quantity-sheet feature.

Built from the resolved design ledger in
documentation/.SESSION_HANDOFFS/dwg_to_qty_sheet/deployment_planning/ (DQ-1..18).

The deterministic geometry layer lives in src/dwg_qty (no LLM calls there).
This package holds everything around it:

    qty_tbl_parser  - parse Terra's qty_tbl_*.txt takeoff sheets into records
                      and derive the canonical closed row-label vocabulary (DQ-7)
    build_corpus    - CLI that regenerates the gitignored corpus/ data files
                      (qty_tbl records, vocabulary, census fingerprints)
    census          - mandatory server-side census run on upload (DQ-6)
    jobs            - per-job folder provisioning under data/dwg_jobs/ (DQ-5/15)
    context         - signatures+docstrings extraction of dwg_qty and the DWG
                      extraction system prompt (DQ-4/8/9/11)
    sandbox         - audit-hook filesystem guard + Windows Job Object resource
                      caps for LLM-written python (DQ-5)
    eval            - agent-driven end-to-end eval fixture + reliability report
                      (DQ-12/13/14)

Corpus data files under corpus/ are derived from Terra Underground business
data and are deliberately NOT committed to git (see .gitignore) — regenerate
with `python -m src.dwg_pipeline.build_corpus`.
"""

__version__ = "0.1.0"
