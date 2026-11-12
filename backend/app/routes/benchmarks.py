"""Human-labelled benchmark datasets evaluated against immutable saved reviews."""

# FastAPI dependency injection uses Depends in endpoint defaults intentionally.
# ruff: noqa: B008

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.database import benchmarks
from app.database.reviews import get_store
from app.routes.auth import current_user

router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])


class BenchmarkCaseInput(BaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    review_id: str | None = Field(default=None, max_length=128)
    labels_complete: bool = False
    ground_truth_signatures: list[str] = Field(default_factory=list, max_length=200)
    ground_truth_outcome: Literal["findings_present", "no_findings"] | None = None
    fix_decisions: dict[str, Literal["accepted", "rejected"]] = Field(default_factory=dict, max_length=100)

    @field_validator("ground_truth_signatures")
    @classmethod
    def signatures_are_unique_bounded(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("Ground-truth signatures must be unique, nonblank, and at most 300 characters.")
        return values

    @model_validator(mode="after")
    def outcome_must_agree_with_complete_finding_labels(self):
        if self.labels_complete and self.ground_truth_outcome is not None:
            has_findings = bool(self.ground_truth_signatures)
            if has_findings != (self.ground_truth_outcome == "findings_present"):
                raise ValueError("Complete finding labels must agree with the human review outcome.")
        return self


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cases: list[BenchmarkCaseInput] = Field(default_factory=list, max_length=100)


class DatasetLabelsUpdate(BaseModel):
    cases: list[BenchmarkCaseInput] = Field(max_length=100)


def _case_dict(item: BenchmarkCaseInput) -> dict[str, Any]:
    return item.model_dump()


def _signature(issue: dict[str, Any]) -> str:
    return "|".join(str(issue.get(key) or "").strip().casefold() for key in ("category", "severity", "file_path"))


def _review_predictions(review: dict[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    issues = review.get("canonical_issues")
    if not isinstance(issues, list) or not issues:
        issues = review.get("findings", [])
    predictions = {_signature(item) for item in issues if isinstance(item, dict)}
    return predictions, [issue for issue in issues if isinstance(issue, dict)]


@router.post("", status_code=201)
def create_dataset(payload: DatasetCreate, user: dict = Depends(current_user)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Dataset name must not be blank.")
    cases = [_case_dict(item) for item in payload.cases]
    if len({case["case_id"] for case in cases}) != len(cases):
        raise HTTPException(status_code=422, detail="Case IDs must be unique within a dataset.")
    return benchmarks.create_dataset(user["user_id"], name, cases)


@router.get("")
def list_datasets(user: dict = Depends(current_user)):
    return benchmarks.list_datasets(user["user_id"])


@router.put("/{dataset_id}/labels")
def update_labels(dataset_id: str, payload: DatasetLabelsUpdate, user: dict = Depends(current_user)):
    if benchmarks.get_dataset(user["user_id"], dataset_id) is None:
        raise HTTPException(status_code=404, detail="Benchmark dataset not found.")
    cases = [_case_dict(item) for item in payload.cases]
    if len({case["case_id"] for case in cases}) != len(cases):
        raise HTTPException(status_code=422, detail="Case IDs must be unique within a dataset.")
    result = benchmarks.replace_cases(user["user_id"], dataset_id, cases)
    if result is None:
        raise HTTPException(status_code=404, detail="Benchmark dataset not found.")
    return result


@router.post("/{dataset_id}/runs", status_code=201)
def create_run(dataset_id: str, user: dict = Depends(current_user)):
    owner_id = user["user_id"]
    dataset = benchmarks.get_dataset(owner_id, dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="Benchmark dataset not found.")
    store = get_store()
    case_results: list[dict[str, Any]] = []
    true_positive = false_positive = false_negative = labeled_cases = skipped_unlabelled = unavailable_predictions = 0
    accepted = rejected = outcome_correct = outcome_labeled = 0
    durations: list[int] = []
    model_latencies: list[int] = []
    total_model_tokens = 0
    token_usage_reviews = 0
    model_configs: dict[str, dict[str, Any]] = {}
    for case in dataset["cases"]:
        if not case.get("labels_complete"):
            skipped_unlabelled += 1
            case_results.append({"case_id": case["case_id"], "status": "missing_labels", "sample_data": False})
            continue
        review_id = case.get("review_id")
        review = store.get_review(review_id, owner_id=owner_id) if review_id else None
        if not review or review.get("is_demo") or review.get("analysis_mode") != "live":
            unavailable_predictions += 1
            case_results.append({"case_id": case["case_id"], "status": "missing_prediction", "sample_data": bool(review and review.get("is_demo"))})
            continue
        labels = set(case.get("ground_truth_signatures", []))
        predicted, issues = _review_predictions(review)
        true_positive += len(labels & predicted)
        false_positive += len(predicted - labels)
        false_negative += len(labels - predicted)
        labeled_cases += 1
        if isinstance(review.get("duration_ms"), int) and review["duration_ms"] >= 0:
            durations.append(review["duration_ms"])
        if isinstance(review.get("model_latency_ms"), int) and review["model_latency_ms"] >= 0:
            model_latencies.append(review["model_latency_ms"])
        usage = review.get("model_usage")
        if isinstance(usage, dict):
            token_value = usage.get("total_tokens")
            if isinstance(token_value, int) and not isinstance(token_value, bool) and token_value >= 0:
                total_model_tokens += token_value
                token_usage_reviews += 1
        configuration = review.get("model_configuration")
        if not isinstance(configuration, dict):
            configuration = {"provider": review.get("model_provider"), "model": review.get("model_name")}
        model_configs[json.dumps(configuration, sort_keys=True, separators=(",", ":"))] = configuration
        decisions = case.get("fix_decisions", {})
        known_fixes = {fix.get("fix_id") for issue in issues for fix in issue.get("suggested_fixes", []) if isinstance(fix, dict)}
        if any(fix_id not in known_fixes for fix_id in decisions):
            raise HTTPException(status_code=422, detail=f"Case {case['case_id']} labels a fix absent from its linked review.")
        accepted += sum(value == "accepted" for value in decisions.values())
        rejected += sum(value == "rejected" for value in decisions.values())
        outcome = "findings_present" if predicted else "no_findings"
        if case.get("ground_truth_outcome") is not None:
            outcome_labeled += 1
            outcome_correct += case["ground_truth_outcome"] == outcome
        case_results.append({
            "case_id": case["case_id"], "status": "measured", "review_id": review_id,
            "ground_truth_signatures": sorted(labels), "predicted_signatures": sorted(predicted),
            "ground_truth_outcome": case.get("ground_truth_outcome"), "predicted_outcome": outcome,
            "fix_decisions": dict(decisions), "model_provider": review.get("model_provider"),
            "model_name": review.get("model_name"), "duration_ms": review.get("duration_ms"),
            "model_latency_ms": review.get("model_latency_ms"), "model_usage": review.get("model_usage"),
            "model_configuration": configuration,
            "sample_data": False,
        })
    completed = bool(dataset["cases"]) and labeled_cases == len(dataset["cases"]) and unavailable_predictions == 0 and skipped_unlabelled == 0
    precision_denominator, recall_denominator = true_positive + false_positive, true_positive + false_negative
    run = {
        "run_id": uuid4().hex, "dataset_id": dataset_id, "dataset_name": dataset["name"],
        "dataset_updated_at": dataset["updated_at"],
        "created_at": datetime.now(UTC).isoformat(), "status": "completed" if completed else "incomplete",
        "source": "saved_live_reviews", "ground_truth_source": "human_labels", "predictions_source": "stored_review_outputs",
        "case_count": len(dataset["cases"]), "labeled_case_count": labeled_cases,
        "missing_label_case_count": skipped_unlabelled, "missing_prediction_case_count": unavailable_predictions,
        "metrics": {"true_positive": true_positive, "false_positive": false_positive, "false_negative": false_negative,
                    "precision": true_positive / precision_denominator if precision_denominator else None,
                    "recall": true_positive / recall_denominator if recall_denominator else None,
                    "fixes_accepted": accepted, "fixes_rejected": rejected,
                    "review_outcome_correct": outcome_correct,
                    "review_outcome_labeled_count": outcome_labeled,
                    "review_outcome_accuracy": outcome_correct / outcome_labeled if outcome_labeled else None,
                    "average_review_duration_ms": round(sum(durations) / len(durations)) if durations else None,
                    "model_latency_ms": round(sum(model_latencies) / len(model_latencies)) if model_latencies else None,
                    "token_usage": total_model_tokens if token_usage_reviews else None,
                    "token_usage_review_count": token_usage_reviews,
                    "token_usage_coverage": token_usage_reviews / labeled_cases if labeled_cases else 0,
                    "cost": None},
        "model_configurations": [model_configs[key] for key in sorted(model_configs)],
        "case_results": case_results,
        "limitations": ["Benchmarks replay saved outputs; they do not rerun models.", "Precision and recall use exact category|severity|file_path signatures supplied by human labelers.", "Token totals include only actual provider usage returned with linked reviews; provider cost is unavailable."],
    }
    benchmarks.save_run(owner_id, dataset_id, run)
    return run


@router.get("/{dataset_id}/runs")
def list_runs(dataset_id: str, user: dict = Depends(current_user)):
    if benchmarks.get_dataset(user["user_id"], dataset_id) is None:
        raise HTTPException(status_code=404, detail="Benchmark dataset not found.")
    return benchmarks.list_runs(user["user_id"], dataset_id)
