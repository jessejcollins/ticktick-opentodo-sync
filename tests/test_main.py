"""The command line's helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ticktick_opentodo_sync.__main__ import memory_limit_mb, peak_memory_mb


def test_peak_memory() -> None:
    assert peak_memory_mb() > 0


@pytest.mark.parametrize(
    ("v2", "v1", "limit"),
    [
        ("134217728\n", None, 128),  # compose's mem_limit: 128m (cgroup v2)
        ("max\n", "134217728", None),  # no limit in v2: v1 isn't consulted
        (None, "134217728\n", 128),  # cgroup v1
        (None, "9223372036854771712", None),  # no limit in v1
        (None, None, None),  # not in a container
    ],
)
def test_memory_limit(tmp_path: Path, v2: str | None, v1: str | None, limit: int | None) -> None:
    files = []
    for name, text in (("v2", v2), ("v1", v1)):
        path = tmp_path / name
        if text is not None:
            path.write_text(text)
        files.append(str(path))
    assert memory_limit_mb(files) == limit
