"""Compare predicted event timestamps with a CSV containing timestamp_seconds."""
import csv

from .game_context import CONTEXT_CONFIDENCE, candidate_key
from .models import ContextResult


def evaluate(predicted: list[float], truth_csv: str, tolerance: float = 3.0) -> dict:
    with open(truth_csv, newline="", encoding="utf-8") as f:
        actual = sorted(float(row.get("timestamp_seconds", row.get("timestamp", ""))) for row in csv.DictReader(f))
    return evaluate_predictions(predicted, actual, tolerance)


def evaluate_predictions(predicted: list[float], actual: list[float], tolerance: float = 3.0) -> dict:
    if tolerance < 0:
        raise ValueError("Timestamp tolerance must be nonnegative")
    actual = sorted(actual)
    remaining = set(range(len(actual)))
    errors = []
    false_positive = 0
    for pred in sorted(predicted):
        candidates = [(abs(pred - actual[i]), i) for i in remaining]
        if candidates and min(candidates)[0] <= tolerance:
            error, idx = min(candidates)
            errors.append(error)
            remaining.remove(idx)
        else:
            false_positive += 1
    tp, fn = len(errors), len(remaining)
    precision = tp / (tp + false_positive) if tp + false_positive else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": precision, "recall": recall,
            "f1": 2*precision*recall/(precision+recall) if precision+recall else 0.0,
            "false_positives": false_positive, "missed_baskets": fn,
            "mean_absolute_timestamp_error_seconds": sum(errors)/len(errors) if errors else None,
            "matched": tp, "truth_count": len(actual)}


def evaluate_accuracy(state: dict, events: list[dict], truth_csv: str, tolerance: float = 3.0) -> dict:
    """Separate detection, outcome, context and automatic-selection losses."""
    from .cli import effective_detections
    from .events import merge_detections
    from .models import Detection
    with open(truth_csv, newline="", encoding="utf-8") as stream:
        labels = list(csv.DictReader(stream))
    if any(row.get("game_context") not in {"game_play", "warmup_or_break"} for row in labels):
        raise ValueError("Accuracy labels require game_play or warmup_or_break")
    truth = [float(row["timestamp_seconds"]) for row in labels if row["game_context"] == "game_play"]
    detections = {key: Detection.model_validate(value) for key, value in state.get("detections", {}).items()}
    candidates = merge_detections(effective_detections(detections))
    contexts = {key: ContextResult.model_validate(value) for key, value in state.get("contexts", {}).items()}
    predictions = {
        "candidates_before_context": [item["timestamp"] for item in candidates],
        "confirmed_before_context": [item["timestamp"] for item in candidates if item["classification"] == "made_basket"],
        "confirmed_after_context": [item["timestamp_seconds"] for item in events if item["classification"] == "made_basket"],
        "automatically_selected": [item["timestamp_seconds"] for item in events if item.get("included", False)],
    }
    return {**{name: evaluate_predictions(values, truth, tolerance) for name, values in predictions.items()},
            "context": evaluate_context(candidates, contexts, truth_csv, tolerance)}


def evaluate_context(candidates: list[dict], results: dict[str, ContextResult], truth_csv: str,
                     tolerance: float = 3.0) -> dict:
    """Compare context decisions with human labels; never treat model output as truth."""
    if tolerance < 0:
        raise ValueError("Timestamp tolerance must be nonnegative")
    with open(truth_csv, newline="", encoding="utf-8") as stream:
        labels = []
        for row in csv.DictReader(stream):
            context = row["game_context"]
            if context not in {"game_play", "warmup_or_break"}:
                raise ValueError("Human labels must be game_play or warmup_or_break")
            labels.append((float(row["timestamp_seconds"]), context))
    remaining = set(range(len(candidates)))
    rows = []
    for timestamp, truth in sorted(labels):
        nearby = [(abs(timestamp - candidates[i]["timestamp"]), i) for i in remaining]
        if not nearby or min(nearby)[0] > tolerance:
            rows.append({"timestamp_seconds": timestamp, "truth": truth, "decision": "no_candidate"})
            continue
        _, index = min(nearby)
        remaining.remove(index)
        result = results.get(candidate_key(candidates[index]), ContextResult())
        definitive = result.checked and result.confidence is not None and result.confidence >= CONTEXT_CONFIDENCE
        decision = result.classification if definitive else "uncertain"
        rows.append({"timestamp_seconds": timestamp, "truth": truth, "decision": decision,
                     "confidence": result.confidence, "checked": result.checked})
    return {
        "labeled_candidates": len(rows),
        "matched_candidates": sum(row["decision"] != "no_candidate" for row in rows),
        "unlabeled_candidates": len(remaining),
        "warmup_baskets_excluded": sum(row["truth"] == "warmup_or_break" and row["decision"] == "warmup_or_break" for row in rows),
        "game_baskets_incorrectly_excluded": sum(row["truth"] == "game_play" and row["decision"] == "warmup_or_break" for row in rows),
        "game_baskets_retained_for_review": sum(row["truth"] == "game_play" and row["decision"] == "uncertain" for row in rows),
        "warmup_baskets_accepted_as_game": sum(row["truth"] == "warmup_or_break" and row["decision"] == "game_play" for row in rows),
        "labels": rows,
    }
