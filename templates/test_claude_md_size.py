"""Keep CLAUDE.md short. Copy into your tests/ folder.

An instruction file that grows gets skimmed: ours went from about 1,100 to over 5,000 words in a month and
agents followed less of it, not more. This test fails if the file passes LIMIT lines, and fails if it grew
since the recorded size (shrink-only). Lower RECORDED_BYTES whenever you cut it.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIMIT_LINES = 200
RECORDED_BYTES = 6000  # set to the file's size today; only ever lower it


def test_claude_md_stays_short():
    text = (ROOT / "CLAUDE.md").read_text()
    lines = text.count("\n") + 1
    assert lines <= LIMIT_LINES, f"CLAUDE.md is {lines} lines (limit {LIMIT_LINES}): move detail to linked docs"
    size = len(text.encode())
    assert size <= RECORDED_BYTES, (
        f"CLAUDE.md grew to {size} bytes (recorded {RECORDED_BYTES}). Cut something before adding something."
    )
