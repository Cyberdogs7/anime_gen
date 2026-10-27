"""Filesystem race-safety helpers used across the studio."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.fsutil import existing_by_mtime
from studio.fsutil import rmtree_hard


def test_existing_by_mtime_skips_vanished_files(tmp_path):
    a = tmp_path / "a.json"
    a.write_text("{}")
    gone = tmp_path / "gone.json"          # never created -> stat raises
    assert existing_by_mtime([gone, a]) == [a]


def test_existing_by_mtime_sorts_oldest_first(tmp_path):
    a = tmp_path / "a.json"
    a.write_text("{}")
    time.sleep(0.01)
    b = tmp_path / "b.json"
    b.write_text("{}")
    assert existing_by_mtime([b, a]) == [a, b]   # [-1] is newest


def test_rmtree_hard_removes_tree_and_tolerates_missing(tmp_path):
    d = tmp_path / "ep"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.json").write_text("{}")
    rmtree_hard(d)
    assert not d.exists()
    rmtree_hard(d)                              # missing path: no-op, no raise
