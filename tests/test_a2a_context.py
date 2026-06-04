from viksa_ai.runtime.a2a import (
    A2A_PAYLOAD_KEY,
    _strip_envelope,
    attach_envelope,
    context,
)


def test_strip_envelope_and_context():
    inputs = {"source": "JFK", A2A_PAYLOAD_KEY: {"run_id": "r1", "parent_step_id": "p1"}}
    cleaned = _strip_envelope(inputs)
    assert A2A_PAYLOAD_KEY not in cleaned
    assert cleaned["source"] == "JFK"
    assert context() == {"run_id": "r1", "parent_step_id": "p1"}


def test_attach_envelope():
    out = attach_envelope({"a": 1}, {"run_id": "x"})
    assert out[A2A_PAYLOAD_KEY] == {"run_id": "x"}
    assert out["a"] == 1
