from fastapi.testclient import TestClient

from app.database.reviews import get_store
from app.main import app


def _register(client, email):
    response = client.post("/api/auth/register", json={"email": email, "password": "correct horse battery staple"})
    assert response.status_code == 201
    return response.json()["user_id"]


def _review(review_id="live-1"):
    return {
        "review_id": review_id, "is_demo": False, "analysis_mode": "live", "status": "completed",
        "model_provider": "openai_compatible", "model_name": "test-model", "duration_ms": 123,
        "model_latency_ms": 45, "model_usage": {"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58},
        "canonical_issues": [{"category": "bug", "severity": "high", "file_path": "src/app.py", "suggested_fixes": []}],
        "findings": [],
    }


def test_human_label_benchmark_metrics_and_incomplete_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "benchmark.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as client:
        owner_id = _register(client, "bench@example.com")
        get_store().save_review(_review(), owner_id=owner_id)
        get_store().save_review({**_review("demo-review"), "is_demo": True, "analysis_mode": "demo"}, owner_id=owner_id)
        dataset = client.post("/api/benchmarks", json={"name": "Human labels", "cases": [
            {"case_id": "labeled", "review_id": "live-1", "labels_complete": True,
             "ground_truth_signatures": ["bug|high|src/app.py", "security|low|src/app.py"],
             "ground_truth_outcome": "findings_present"},
            {"case_id": "unlabeled", "review_id": "live-1", "labels_complete": False},
            {"case_id": "sample", "review_id": "demo-review", "labels_complete": True, "ground_truth_signatures": []},
        ]})
        assert dataset.status_code == 201
        run_response = client.post(f"/api/benchmarks/{dataset.json()['dataset_id']}/runs")
        assert run_response.status_code == 201
        run = run_response.json()
        assert run["status"] == "incomplete"
        assert run["ground_truth_source"] == "human_labels"
        assert run["predictions_source"] == "stored_review_outputs"
        assert run["metrics"]["true_positive"] == 1
        assert run["metrics"]["false_positive"] == 0
        assert run["metrics"]["false_negative"] == 1
        assert run["metrics"]["precision"] == 1
        assert run["metrics"]["recall"] == 0.5
        assert run["metrics"]["average_review_duration_ms"] == 123
        assert run["metrics"]["model_latency_ms"] == 45
        assert run["metrics"]["token_usage"] == 58 and run["metrics"]["token_usage_review_count"] == 1
        assert run["metrics"]["cost"] is None
        assert run["model_configurations"] == [{"provider": "openai_compatible", "model": "test-model"}]
        assert [case["status"] for case in run["case_results"]] == ["measured", "missing_labels", "missing_prediction"]
        assert client.get(f"/api/benchmarks/{dataset.json()['dataset_id']}/runs").json()[0]["run_id"] == run["run_id"]


def test_benchmark_datasets_are_owner_scoped_and_case_ids_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "benchmark-scope.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as owner, TestClient(app) as other:
        _register(owner, "bench-owner@example.com")
        _register(other, "bench-other@example.com")
        created = owner.post("/api/benchmarks", json={"name": "Private", "cases": []})
        assert created.status_code == 201
        dataset_id = created.json()["dataset_id"]
        assert other.get("/api/benchmarks").json() == []
        assert other.post(f"/api/benchmarks/{dataset_id}/runs").status_code == 404
        duplicate = owner.post("/api/benchmarks", json={"name": "Bad", "cases": [{"case_id": "same"}, {"case_id": "same"}]})
        assert duplicate.status_code == 422
        contradictory = owner.post("/api/benchmarks", json={"name": "Contradictory", "cases": [
            {"case_id": "none", "labels_complete": True, "ground_truth_signatures": [], "ground_truth_outcome": "findings_present"},
        ]})
        assert contradictory.status_code == 422


def test_benchmark_rejects_decision_label_for_unlinked_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "benchmark-fix.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as client:
        owner_id = _register(client, "benchmark-fix@example.com")
        get_store().save_review(_review(), owner_id=owner_id)
        dataset = client.post("/api/benchmarks", json={"name": "Bad fix label", "cases": [
            {"case_id": "one", "review_id": "live-1", "labels_complete": True, "fix_decisions": {"invented": "accepted"}},
        ]}).json()
        response = client.post(f"/api/benchmarks/{dataset['dataset_id']}/runs")
        assert response.status_code == 422


def test_benchmark_dataset_and_run_round_trip_through_mongodb_adapter(monkeypatch):
    import mongomock

    from app.database import benchmarks

    mongo = mongomock.MongoClient(tz_aware=True)["benchmark-test"]
    monkeypatch.setattr(benchmarks, "_mongo", lambda: mongo)
    monkeypatch.setenv("PR_MENTOR_STORAGE", "mongodb")
    dataset = benchmarks.create_dataset("owner", "Mongo labels", [{"case_id": "a", "labels_complete": True}])
    assert benchmarks.get_dataset("owner", dataset["dataset_id"])["cases"] == [{"case_id": "a", "labels_complete": True}]
    assert benchmarks.replace_cases("owner", dataset["dataset_id"], [{"case_id": "b", "labels_complete": False}])["cases"] == [{"case_id": "b", "labels_complete": False}]
    run = {"run_id": "run-1", "created_at": "2026-11-08T00:00:00Z", "case_results": [{"predicted_signatures": ["bug|high|a.py"]}]}
    benchmarks.save_run("owner", dataset["dataset_id"], run)
    assert benchmarks.list_runs("owner", dataset["dataset_id"]) == [run]
