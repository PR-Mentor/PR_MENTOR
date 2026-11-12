from fastapi.testclient import TestClient

from app.data.sample_reviews import sample_review
from app.database.reviews import get_store
from app.main import app


def _register(client, email):
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    return response.json()


def _workspace(client, name="Team Alpha"):
    response = client.post("/api/workspaces", json={"name": name})
    assert response.status_code == 201
    return response.json()


def test_workspace_invitation_sharing_and_cross_workspace_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "workspace.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as owner, TestClient(app) as editor, TestClient(app) as outsider:
        owner_user = _register(owner, "owner@example.com")
        editor_user = _register(editor, "editor@example.com")
        _register(outsider, "outsider@example.com")
        workspace = _workspace(owner)
        workspace_id = workspace["workspace_id"]

        invitation = owner.post(
            f"/api/workspaces/{workspace_id}/invitations",
            json={"email": "editor@example.com", "role": "editor"},
        )
        assert invitation.status_code == 201
        accepted = editor.post(
            "/api/workspaces/invitations/accept",
            json={"token": invitation.json()["token"]},
        )
        assert accepted.status_code == 200
        assert accepted.json()["role"] == "editor"

        review = sample_review("acme", "calculator", 1).model_dump(mode="json")
        review["review_id"] = "shared-review"
        get_store().save_review(review, owner_id=owner_user["user_id"])
        shared = owner.post(f"/api/workspaces/{workspace_id}/reviews/shared-review")
        assert shared.status_code == 201

        assert editor.get("/api/reviews/shared-review").status_code == 200
        from app.services.review_access import resolve_review_access

        shared_review, shared_role = resolve_review_access(
            "shared-review", editor_user["user_id"], write=True
        )
        assert shared_review is not None and shared_role == "editor"
        get_store().save_review(
            {**review, "summary": "Updated by workspace editor."},
            owner_id=editor_user["user_id"],
        )
        assert get_store().get_review("shared-review", owner_id=owner_user["user_id"])["summary"] == "Updated by workspace editor."
        assert get_store().get_review("shared-review", owner_id=editor_user["user_id"]) is None
        workspace_history = editor.get(f"/api/workspaces/{workspace_id}/reviews")
        assert workspace_history.status_code == 200
        assert [item["review_id"] for item in workspace_history.json()] == ["shared-review"]
        assert outsider.get("/api/reviews/shared-review").status_code == 404
        assert outsider.get(f"/api/workspaces/{workspace_id}").status_code == 404
        denied_job = outsider.post(
            "/api/reviews/jobs",
            json={
                "url": "https://github.com/acme/calculator/pull/1",
                "mode": "live",
                "workspace_id": workspace_id,
            },
        )
        assert denied_job.status_code == 404


def test_viewer_can_read_but_cannot_mutate_shared_review(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "workspace-viewer.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as owner, TestClient(app) as viewer:
        owner_user = _register(owner, "owner@example.com")
        _register(viewer, "viewer@example.com")
        workspace_id = _workspace(owner)["workspace_id"]
        invitation = owner.post(
            f"/api/workspaces/{workspace_id}/invitations",
            json={"email": "viewer@example.com", "role": "viewer"},
        ).json()
        assert viewer.post("/api/workspaces/invitations/accept", json={"token": invitation["token"]}).status_code == 200

        review = sample_review("acme", "calculator", 1).model_dump(mode="json")
        review["review_id"] = "viewer-shared-review"
        get_store().save_review(review, owner_id=owner_user["user_id"])
        assert owner.post(f"/api/workspaces/{workspace_id}/reviews/viewer-shared-review").status_code == 201
        assert viewer.get("/api/reviews/viewer-shared-review").status_code == 200
        denied = viewer.post(
            "/api/reviews/viewer-shared-review/issues/missing/fixes/missing/decision",
            json={"decision": "accepted"},
        )
        assert denied.status_code == 403


def test_invitation_is_bound_to_email_and_owner_cannot_be_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STORAGE", "sqlite")
    monkeypatch.setenv("PR_MENTOR_DATABASE_PATH", str(tmp_path / "workspace-invite.sqlite3"))
    monkeypatch.setenv("PR_MENTOR_COOKIE_SECURE", "false")
    with TestClient(app) as owner, TestClient(app) as wrong_user, TestClient(app) as invitee:
        _register(owner, "owner@example.com")
        _register(wrong_user, "wrong@example.com")
        invitee_user = _register(invitee, "invited@example.com")
        workspace_id = _workspace(owner)["workspace_id"]
        invitation = owner.post(
            f"/api/workspaces/{workspace_id}/invitations",
            json={"email": "invited@example.com", "role": "viewer"},
        ).json()
        assert wrong_user.post("/api/workspaces/invitations/accept", json={"token": invitation["token"]}).status_code == 404
        assert invitee.post("/api/workspaces/invitations/accept", json={"token": invitation["token"]}).status_code == 200
        assert invitee.post("/api/workspaces/invitations/accept", json={"token": invitation["token"]}).status_code == 404
        assert owner.delete(f"/api/workspaces/{workspace_id}/members/{invitee_user['user_id']}").status_code == 200
        owner_id = owner.get("/api/auth/me").json()["user_id"]
        assert owner.delete(f"/api/workspaces/{workspace_id}/members/{owner_id}").status_code == 409
