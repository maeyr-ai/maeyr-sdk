from __future__ import annotations

from importlib.resources import files

import maeyr_platform


def test_version_and_pep561_marker_are_packaged() -> None:
    assert maeyr_platform.__version__ == "0.2.1"
    assert files("maeyr_platform").joinpath("py.typed").is_file()
