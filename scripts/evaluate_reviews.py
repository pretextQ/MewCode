"""Score saved review predictions against the small, provisional offline case set.

No model calls. Labels need maintainer review before use as a quality benchmark.
Predictions are a JSON object keyed by case ID, with ReviewResult-shaped values.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CASES = Path(__file__).resolve().parents[1] / "tests/fixtures/independent_review_cases.json"


def evaluate(cases: list[dict], predictions: dict[str, dict]) -> dict:
    if set(predictions) != {case["id"] for case in cases}:
        raise ValueError("predictions must contain exactly the dataset case IDs")
    tp = fp = fn = tn = incomplete = unassessed_negative = 0
    input_tokens = output_tokens = 0
    elapsed = 0.0
    for case in cases:
        value = predictions[case["id"]]
        if value.get("status") not in ("findings", "no_findings", "incomplete"):
            raise ValueError("prediction status must be findings, no_findings or incomplete")
        detected = bool(value.get("findings"))
        unfinished = value["status"] == "incomplete" or not value.get("coverage_complete", False)
        incomplete += unfinished
        if case["expected_issue"]:
            tp += detected
            fn += not detected
        else:
            fp += detected
            tn += not detected and not unfinished
            unassessed_negative += not detected and unfinished
        input_tokens += int(value.get("input_tokens", 0))
        output_tokens += int(value.get("output_tokens", 0))
        elapsed += float(value.get("elapsed_seconds", 0))
    return {
        "cases": len(cases), "true_positives": tp, "false_positives": fp,
        "missed_positives": fn, "true_negatives": tn, "incomplete": incomplete,
        "unassessed_negatives": unassessed_negative,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "elapsed_seconds": elapsed,
        "notes": ["issue presence only; finding correctness requires human adjudication",
                  "incomplete positives without findings count as missed; not silently excluded",
                  "labels are provisional; fixture predictions do not demonstrate model effectiveness"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(CASES.read_text(encoding="utf-8")),
                              json.loads(args.predictions.read_text(encoding="utf-8"))), indent=2))


if __name__ == "__main__":
    main()
