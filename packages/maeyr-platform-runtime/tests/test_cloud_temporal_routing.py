import pytest

from maeyr_platform.temporal_routing import cloud_worker_task_queue


def test_cloud_queue_is_stable_bounded_and_separates_every_tenant_scope():
    expected = cloud_worker_task_queue("AC-one", "OI-same", "PI-same")
    assert expected == cloud_worker_task_queue("AC-one", "OI-same", "PI-same")
    assert len(expected) == 70
    assert len({expected, cloud_worker_task_queue("AC-two", "OI-same", "PI-same"),
                cloud_worker_task_queue("AC-one", "OI-other", "PI-same"),
                cloud_worker_task_queue("AC-one", "OI-same", "PI-other")}) == 4
    assert cloud_worker_task_queue("a-b", "c", "d") != cloud_worker_task_queue("a", "b-c", "d")


@pytest.mark.parametrize("value", [None, 1, "", " ", "AC-one ", "AC\x00one", "x" * 257])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_invalid_queue_scope_fails_closed(value, position):
    scope = ["AC-one", "OI-one", "PI-one"]
    scope[position] = value
    with pytest.raises(ValueError):
        cloud_worker_task_queue(*scope)
