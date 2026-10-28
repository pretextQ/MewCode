"""Reject false RED evidence without launching mutation subprocesses."""
import pytest

from scripts.verify_restored_defects import DEFECTS, valid_evidence


@pytest.mark.parametrize("change", [
    {"exception": "ImportError"}, {"exception": "SyntaxError"},
    {"exception": "TimeoutError"}, {"assertion": "assert unrelated"},
    {"when": "setup"}, {"outcome": "skipped"}, {"file": "other.py"},
    {"node": "tests/other.py::test_other"},
    {"node": DEFECTS[0].test + "_unrelated"},
])
def test_invalid_red_is_rejected(change):
    defect = DEFECTS[0]
    event = {
        "node": defect.test + "[parameter]", "when": "call", "outcome": "failed",
        "exception": "AssertionError", "assertion": defect.assertion,
        "file": defect.test.split("::")[0],
    }
    assert valid_evidence({"code": 1, "events": [event]}, defect, restored=True)
    event.update(change)
    assert not valid_evidence({"code": 1, "events": [event]}, defect, restored=True)


def test_empty_or_collection_error_is_not_green_or_red():
    for restored in (False, True):
        for code in (0, 1, 2):
            assert not valid_evidence({"code": code, "events": []}, DEFECTS[0], restored=restored)
