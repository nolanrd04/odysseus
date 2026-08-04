"""
Agent context assembly for the DWG extraction flow.

Two jobs:
  1. `dwg_qty_signatures()` — the DQ-4 decision made real: an ast-derived
     signatures+docstrings-only rendering of src/dwg_qty (~1/5 the tokens of
     raw source, preserving the empirically-hard-won reasoning that lives in
     docstrings). The model escapes this curated view via
     `inspect.getsource(...)` or ad hoc code when it hits a gap — intended
     behavior, not a limitation.
  2. `build_dwg_system_prompt(...)` — the full system-prompt addendum for a
     DWG extraction turn: field rules (DWG_RULES.md), library reference,
     the job's already-run census (DQ-6), the qty_tbl vocabulary (DQ-7),
     ghost-firm archetype instructions (DQ-8), Case A/B output contract
     (DQ-9), and forced-vs-confirm intent behavior (DQ-2).

The assembled prompt is stable per job (census JSON included verbatim), so
odysseus's existing 5-minute ephemeral prompt cache picks it up automatically
(llm_core writes a cache_control breakpoint on system blocks >4k chars).
"""

from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path

_PIPELINE_DIR = Path(__file__).resolve().parent
_DWG_QTY_DIR = _PIPELINE_DIR.parent / "dwg_qty"
CORPUS_DIR = _PIPELINE_DIR / "corpus"
PROMPTS_DIR = _PIPELINE_DIR / "prompts"

_MODULE_ORDER = [
    "convert",
    "census",
    "annotation",
    "length",
    "area",
    "proximity",
    "centerline",
    "boundary",
]


def _format_signature(node: ast.FunctionDef) -> str:
    return f"def {node.name}({ast.unparse(node.args)})" + (
        f" -> {ast.unparse(node.returns)}:" if node.returns else ":"
    )


def _extract_module(path: Path) -> str:
    """Signatures + docstrings for one module, implementation bodies stripped."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = [f"### src.dwg_qty.{path.stem}"]

    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        out.append(f'"""{mod_doc}"""')

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            out.append(f"class {node.name}({bases}):" if bases else f"class {node.name}:")
            cls_doc = ast.get_docstring(node)
            if cls_doc:
                out.append(f'    """{cls_doc}"""')
            for item in node.body:
                if isinstance(item, ast.AnnAssign):
                    out.append(f"    {ast.unparse(item)}")
                elif isinstance(item, ast.FunctionDef) and not item.name.startswith("_"):
                    out.append(f"    {_format_signature(item)}")
                    fn_doc = ast.get_docstring(item)
                    if fn_doc:
                        out.append(f'        """{fn_doc}"""')
        elif isinstance(node, ast.FunctionDef):
            if node.name.startswith("_"):
                continue
            out.append(_format_signature(node))
            fn_doc = ast.get_docstring(node)
            if fn_doc:
                out.append(f'    """{fn_doc}"""')
        elif isinstance(node, ast.Assign):
            # module-level constants (e.g. DEFAULT_TARGET_VERSION) are part of the API
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if targets and all(t.isupper() for t in targets):
                out.append(ast.unparse(node))
    return "\n".join(out)


@lru_cache(maxsize=1)
def dwg_qty_signatures() -> str:
    """Signatures+docstrings-only rendering of the whole dwg_qty package."""
    parts = ['## dwg_qty library reference (import as `from src.dwg_qty.<module> import ...`)']
    init_doc = ast.get_docstring(ast.parse((_DWG_QTY_DIR / "__init__.py").read_text(encoding="utf-8")))
    if init_doc:
        parts.append(f'"""{init_doc}"""')
    for name in _MODULE_ORDER:
        parts.append(_extract_module(_DWG_QTY_DIR / f"{name}.py"))
    return "\n\n".join(parts)


@lru_cache(maxsize=1)
def field_rules() -> str:
    return (_PIPELINE_DIR / "DWG_RULES.md").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def load_vocabulary() -> list[dict]:
    path = CORPUS_DIR / "vocabulary.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def vocabulary_block() -> str:
    """Compact rendering of the canonical row-label vocabulary with stats."""
    vocab = load_vocabulary()
    if not vocab:
        return _load_prompt("vocabulary_block_not_built.txt")
    lines = [
        _load_prompt("vocabulary_block_header.txt"),
        "",
        "| group | work_type | units | jobs_present | jobs_populated |",
        "|:--|:--|:--|--:|--:|",
    ]
    for e in vocab:
        units = "/".join(e["units"])
        lines.append(
            f"| {e['group']} | {e['work_type']} | {units} | {e['jobs_present']} | {e['jobs_populated']} |"
        )
    return "\n".join(lines)


def vocabulary_template_block() -> str:
    """Blank fill-in-the-blank rendering of the closed vocabulary (DQ-17) —
    the literal table shape the model must populate, not just a reference.

    This was dead code (qty_tbl_parser.vocabulary_as_template existed but
    was never wired into the live prompt) until 2026-08-01: models were
    freehand-composing their own paraphrased/merged row labels instead of
    copying Terra's exact template text, so live-run scoring
    (src/dwg_pipeline/generations.py) was showing near-zero label matches
    against corpus ground truth even when quantities looked reasonable.
    Wiring this in gives the model the actual rows to fill in, not just
    stats about them (vocabulary_block() above stays as-is — its
    jobs_present/jobs_populated counts are still useful judgment context).
    """
    from src.dwg_pipeline.qty_tbl_parser import vocabulary_as_template

    vocab = load_vocabulary()
    if not vocab:
        return ""
    return (
        f"{_load_prompt('vocabulary_template_instructions.txt')}\n\n"
        f"{vocabulary_as_template(vocab)}"
    )


def build_dwg_system_prompt(
    census_payload: dict,
    job_dir: str,
    dxf_files: list[str],
    forced: bool,
) -> str:
    """
    Full system-prompt addendum for a DWG extraction turn.

    `forced` — True when the request came through the sidebar DWG-extraction
    modal (extraction is assumed); False when a .dwg was attached to normal
    chat (confirm intent before running extraction, per DQ-2).
    """
    intent = _load_prompt(
        "intent_forced.txt" if forced else "intent_attached.txt"
    )

    dxf_list = "\n".join(f"  - {name}" for name in dxf_files)

    return _load_prompt("dwg_system_prompt.txt").format(
        intent=intent,
        job_dir=job_dir,
        dxf_list=dxf_list,
        field_rules=field_rules(),
        dwg_qty_signatures=dwg_qty_signatures(),
        vocabulary_block=vocabulary_block(),
        vocabulary_template=vocabulary_template_block(),
        census_json=json.dumps(census_payload, indent=1),
    )
