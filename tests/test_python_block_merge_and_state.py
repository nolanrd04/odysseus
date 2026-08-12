"""Multi-block python replies run as one script; NameError explains itself.

Every ```python block used to be its own `python -I -c` process, so a name
defined in one block was gone in the next — while the agent rules told the model
"Multiple tool blocks per response OK" and never mentioned the isolation. A live
run wrote 31 blocks against a 20-round cap and 20 of them referenced names they
never defined; one variable was chased across seven separate blocks.
"""

import asyncio

import pytest

import src.agent_tools as _at  # noqa: F401  (import package first — breaks a cycle)
from src.agent_tools import ToolBlock
from src.agent_loop import _merge_python_blocks
from src.agent_tools.subprocess_tools import PythonTool, _stateless_state_hint


def _types(blocks):
    return [b.tool_type for b in blocks]


def _run(code):
    return asyncio.run(PythonTool().execute(code, {}))


class TestMergeSelection:
    def test_adjacent_python_blocks_merge(self):
        merged = _merge_python_blocks([ToolBlock("python", "a=1"), ToolBlock("python", "print(a)")])
        assert _types(merged) == ["python"]

    def test_a_lone_block_is_passed_through_untouched(self):
        # No wrapper for the common case — merging must not change a single
        # block's source, line numbers, or tracebacks.
        only = ToolBlock("python", "print(1)")
        assert _merge_python_blocks([only])[0].content == "print(1)"

    def test_blocks_separated_by_another_tool_do_not_merge(self):
        # `python, read_file, python` must keep the file read BETWEEN them;
        # merging would silently reorder execution.
        seq = [ToolBlock("python", "a=1"), ToolBlock("read_file", "x.txt"), ToolBlock("python", "print(a)")]
        assert _types(_merge_python_blocks(seq)) == ["python", "read_file", "python"]

    def test_only_the_adjacent_run_merges(self):
        seq = [
            ToolBlock("python", "a=1"), ToolBlock("python", "b=2"),
            ToolBlock("bash", "ls"), ToolBlock("python", "c=3"),
        ]
        assert _types(_merge_python_blocks(seq)) == ["python", "bash", "python"]

    def test_non_python_tools_are_never_merged(self):
        seq = [ToolBlock("bash", "ls"), ToolBlock("bash", "pwd")]
        assert _types(_merge_python_blocks(seq)) == ["bash", "bash"]

    def test_merged_source_survives_quotes_backslashes_and_unicode(self):
        # Blocks are embedded with repr(), so no amount of quoting in the
        # model's code can break out of the wrapper.
        nasty = "s = '''triple \\\\ \"quotes\" → ok'''\nprint(len(s))"
        merged = _merge_python_blocks([ToolBlock("python", nasty), ToolBlock("python", "print(s[:3])")])[0]
        compile(merged.content, "<merged>", "exec")  # raises if repr() mangled it


class TestMergedExecution:
    def test_state_is_shared_between_merged_blocks(self):
        merged = _merge_python_blocks([
            ToolBlock("python", "from collections import Counter\nvals = [1, 2, 3]"),
            ToolBlock("python", "print('sum:', sum(vals)); print('counter:', Counter('aab')['a'])"),
        ])[0]
        out = _run(merged.content)["output"]
        assert "sum: 6" in out
        assert "counter: 2" in out

    def test_a_failing_block_does_not_skip_later_blocks(self):
        # The property that makes merging safe. Plain text concatenation would
        # let the first error abort everything after it — strictly worse than
        # the per-process behaviour it replaces, on a run where most blocks fail.
        merged = _merge_python_blocks([
            ToolBlock("python", "x = 5"),
            ToolBlock("python", "raise ValueError('boom')"),
            ToolBlock("python", "print('later block ran, x =', x)"),
        ])[0]
        result = _run(merged.content)
        assert "later block ran, x = 5" in result["output"]
        assert "ValueError: boom" in result["output"]

    def test_merged_traceback_names_the_offending_block(self):
        merged = _merge_python_blocks([
            ToolBlock("python", "print('ok')"),
            ToolBlock("python", "raise RuntimeError('second')"),
        ])[0]
        assert "<block 2>" in _run(merged.content)["output"]


class TestStatelessHint:
    def test_hint_names_the_missing_variable(self):
        hint = _stateless_state_hint("NameError: name 'parsed_rows' is not defined")
        assert "`parsed_rows`" in hint
        assert "brand-new process" in hint

    def test_no_hint_when_there_is_no_nameerror(self):
        assert _stateless_state_hint("ValueError: nope") == ""
        assert _stateless_state_hint("") == ""

    def test_real_nameerror_reaches_the_model_through_the_tool(self):
        out = _run("print(undefined_from_an_earlier_call)")["output"]
        assert "[odysseus]" in out
        assert "`undefined_from_an_earlier_call`" in out


class TestChildEncoding:
    def test_non_cp1252_output_does_not_kill_the_script(self):
        # `-I` implies `-E`, so PYTHONIOENCODING is ignored and the child fell
        # back to the Windows locale encoding: printing an arrow raised
        # UnicodeEncodeError and destroyed everything computed so far. This
        # killed the first script of a live agent run.
        result = _run("print('report → summary — 90° ✓')\nprint('survived')")
        assert result["exit_code"] == 0
        assert "survived" in result["output"]
        assert "UnicodeEncodeError" not in result["output"]

    def test_unicode_round_trips_intact(self):
        # Child encodes UTF-8 and the readers decode UTF-8 — no mojibake.
        assert "→" in _run("print('a → b')")["output"]
