"""Offline fitting and replay using exactly the production feature contract.

Input JSON: {"surname_counts": {...}, "rows": [{"evidence": {...},
"label": 0|1, "origin": "annotated"|"synthetic"}]}. Synthetic records are
training-only. No database, model API, or automatic artifact replacement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pauk.graph.person_resolution import (  # noqa: E402
    MODEL_FEATURES,
    PairEvidence,
    ResolverPolicy,
    feature_vector,
    resolve_pair,
)
from pauk.graph.person_resolution_model import DEFAULT_MODEL_PATH, load_logistic_model  # noqa: E402


def read_dataset(path: Path) -> tuple[dict, list[PairEvidence], list[int], list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    counts = payload["surname_counts"]
    if not isinstance(counts, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0 for key, value in counts.items()
    ):
        raise ValueError("surname_counts must contain nonnegative integer counts")
    items, labels, origins = [], [], []
    for row in payload["rows"]:
        if type(row["label"]) is not int or row["label"] not in (0, 1):
            raise ValueError("labels must be 0 or 1")
        if row["origin"] not in ("annotated", "synthetic"):
            raise ValueError("origin must be annotated or synthetic")
        items.append(PairEvidence(**row["evidence"]))
        labels.append(row["label"])
        origins.append(row["origin"])
    if not items:
        raise ValueError("dataset is empty")
    return counts, items, labels, origins


def train(counts, items, labels, *, c_value=0.03, seed=20260921):
    import numpy as np
    import sklearn
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    vectors = [feature_vector(item, counts) for item in items]
    matrix = [[vector[name] for name in MODEL_FEATURES] for vector in vectors]
    fitted = make_pipeline(
        StandardScaler(),
        LogisticRegression(C=c_value, solver="liblinear", class_weight={0: 1.0, 1: 1.0}, max_iter=5000, random_state=seed),
    ).fit(np.asarray(matrix), labels)
    scaler, logistic = fitted.named_steps["standardscaler"], fitted.named_steps["logisticregression"]
    return {
        "format_version": 1, "model_type": "standardized_logistic_regression",
        "feature_contract": "roles_v3_raw56", "feature_names": MODEL_FEATURES,
        "means": tuple(map(float, scaler.mean_)), "scales": tuple(map(float, scaler.scale_)),
        "coefficients": tuple(map(float, logistic.coef_[0])), "intercept": float(logistic.intercept_[0]),
        "surname_counts": counts,
        "training": {"C": c_value, "seed": seed, "solver": "liblinear", "sklearn": sklearn.__version__, "numpy": np.__version__},
    }


def replay(items, labels, model, policy):
    resolutions = [resolve_pair(item, policy, model) for item in items]
    decisions = Counter(result.decision.value for result in resolutions)
    false_merge = sum(result.decision.value == "merge" and label == 0 for result, label in zip(resolutions, labels, strict=True))
    false_separate = sum(result.decision.value == "separate" and label == 1 for result, label in zip(resolutions, labels, strict=True))
    return {
        "pairs": len(items), "decisions": dict(decisions),
        "false_merge_against_labels": false_merge, "false_separate_against_labels": false_separate,
        "false_merge_percent_of_merges": 100 * false_merge / decisions["merge"] if decisions["merge"] else None,
        "false_separate_percent_of_labelled_duplicates": 100 * false_separate / sum(labels) if sum(labels) else None,
        "predictions": [
            {"ids": [item.person_a, item.person_b], "label": label, "probability": result.probability,
             "decision": result.decision.value, "route": result.route}
            for item, label, result in zip(items, labels, resolutions, strict=True)
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--fit-output", type=Path, help="Fit to a new file; existing artifacts are never overwritten")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--separate-below", type=float, default=0.13)
    parser.add_argument("--merge-from", type=float, default=0.94)
    args = parser.parse_args()
    policy = ResolverPolicy(args.separate_below, args.merge_from)
    counts, items, labels, origins = read_dataset(args.dataset)
    model_path = args.model
    if args.fit_output:
        payload = train(counts, items, labels)
        payload["training"]["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
        model_path = args.fit_output
        with model_path.open("xb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    model = load_logistic_model(model_path, MODEL_FEATURES)
    selected = [index for index, origin in enumerate(origins) if origin == "annotated"]
    result = replay([items[i] for i in selected], [labels[i] for i in selected], model, policy)
    result.update(
        evaluation="Label replay, not independent validation; Qwen is not called.", paid_calls=0,
        model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
        dataset_sha256=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        thresholds={"separate_below": policy.separate_below, "merge_from": policy.merge_from},
    )
    with args.report.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in result.items() if key != "predictions"}, indent=2))


if __name__ == "__main__":
    main()
