import json
from datetime import UTC, datetime, timedelta

from app.test_execution import reaper


def test_orphan_reaper_removes_only_old_owned_runtime_containers(monkeypatch):
    now = datetime(2026, 11, 8, tzinfo=UTC)
    ids = ["a" * 64, "b" * 64, "c" * 64]
    removed = []

    def docker(args, *, timeout=8.0):
        if args[:3] == ["ps", "-aq", "--filter"]:
            return "\n".join(ids)
        if args[0] == "inspect":
            container_id = args[-1]
            stale = container_id == ids[0]
            return json.dumps(
                {
                    "Name": "/prmentor-test-" + container_id[:8],
                    "Created": (
                        now - timedelta(minutes=10 if stale else 1)
                    ).isoformat(),
                    "Config": {
                        "Labels": {
                            reaper.CONTAINER_LABEL: reaper.CONTAINER_VALUE
                            if container_id != ids[2]
                            else "other"
                        }
                    },
                }
            )
        if args[:2] == ["rm", "--force"]:
            removed.append(args[2])
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(reaper, "_docker", docker)
    assert reaper.reap_orphan_containers(now=now) == 1
    assert removed == [ids[0]]


def test_artifact_reaper_is_bounded_to_aged_prmentor_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_MENTOR_STAGE6_TEMP_ROOT", str(tmp_path))
    stale = tmp_path / (reaper.TEMP_PREFIX + "old")
    fresh = tmp_path / (reaper.TEMP_PREFIX + "fresh")
    unrelated = tmp_path / "other-temp"
    for item in (stale, fresh, unrelated):
        item.mkdir()
    old = datetime.now(UTC) - timedelta(hours=2)
    import os

    os.utime(stale, (old.timestamp(), old.timestamp()))
    assert reaper.reap_stale_artifacts() == 1
    assert not stale.exists()
    assert fresh.exists() and unrelated.exists()
