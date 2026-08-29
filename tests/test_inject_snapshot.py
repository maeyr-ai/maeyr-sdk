from pathlib import Path

from maeyr.runtime.inject import to_module_source

FIXTURE = Path(__file__).parent / "fixtures" / "Maeyr.py.expected"


def test_to_module_source_matches_platform_fixture():
    generated = to_module_source()
    expected = FIXTURE.read_text()
    assert generated == expected, (
        "Injected SDK drifted from platform fixture. "
        f"len generated={len(generated)} expected={len(expected)}"
    )
