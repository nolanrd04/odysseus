"""
Agent-driven end-to-end eval fixture + reliability report (DQ-12/13/14).

`run_eval` runs the REAL extraction agent (same stream_agent_loop, same DWG
system prompt, same sandboxed python tool) against a job whose completed
qty_tbl exists on disk, then diffs the agent's predicted table row-by-row
against the real one. Evaluation only — results are never fed back into the
run for self-correction (DQ-10: no QC loop).

Per-run metrics (DQ-12): 10% per-line tolerance, overall + per-line distance
from actual, misses (Case B) and extras (Case A). Persisted as JSON under
DATA_DIR/dwg_eval_runs/; `report` aggregates all runs into the DQ-14
actuals-reliability report.
"""
