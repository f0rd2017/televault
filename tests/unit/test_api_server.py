from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from app.api.server import ApiContext, ApiServer, dispatch
from app.core.types import ApiConfig, FolderEntry, ObjectEntry


class FakeRepo:
    def __init__(self) -> None:
        self.folders = [FolderEntry(folder_path="Docs", created_ts=10, pinned=0)]
        self.objects = [
            ObjectEntry(
                file_key="k1",
                folder_path="Docs",
                orig_name="a.txt",
                parts_total=1,
                have_parts=1,
                status="complete",
                total_size=100,
                last_seen_ts=5,
            )
        ]
        self.jobs = [
            {
                "id": 7,
                "type": "upload",
                "payload": {},
                "status": "done",
                "progress": 1.0,
                "created_ts": 1,
                "updated_ts": 2,
                "error_text": None,
            }
        ]
        self.unified_calls: list[tuple] = []

    def list_folders(self):
        return list(self.folders)

    def list_objects_unified(self, folder, search, status, *, recursive=False):
        self.unified_calls.append((folder, search, status, recursive))
        return list(self.objects)

    def list_jobs(self, limit=100):
        return list(self.jobs)

    def get_job(self, job_id):
        for j in self.jobs:
            if j["id"] == int(job_id):
                return j
        return None


class FakeWorker:
    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.submitted: list[tuple[str, dict]] = []

    def submit_job(self, job_type, payload) -> bool:
        self.submitted.append((job_type, dict(payload)))
        return self.accept


def _ctx(
    token: str = "", accept: bool = True
) -> tuple[ApiContext, FakeRepo, FakeWorker]:
    repo = FakeRepo()
    worker = FakeWorker(accept=accept)
    return ApiContext(repo=repo, worker=worker, token=token), repo, worker


def _call(ctx, method, path, *, query=None, headers=None, body=None):
    q = {k: [v] for k, v in (query or {}).items()}
    raw = json.dumps(body).encode() if body is not None else b""
    return dispatch(ctx, method, path, q, headers or {}, raw)


# ── Авторизация ──────────────────────────────────────────────────────────────


def test_health_is_public():
    ctx, _, _ = _ctx(token="secret")
    status, payload = _call(ctx, "GET", "/api/health")
    assert status == 200
    assert payload["status"] == "ok"


def test_auth_required_when_token_set():
    ctx, _, _ = _ctx(token="secret")
    status, payload = _call(ctx, "GET", "/api/folders")
    assert status == 401
    assert "error" in payload


def test_auth_accepts_bearer_header():
    ctx, _, _ = _ctx(token="secret")
    status, _ = _call(
        ctx, "GET", "/api/folders", headers={"Authorization": "Bearer secret"}
    )
    assert status == 200


def test_auth_accepts_query_token():
    ctx, _, _ = _ctx(token="secret")
    status, _ = _call(ctx, "GET", "/api/folders", query={"token": "secret"})
    assert status == 200


def test_auth_rejects_wrong_token():
    ctx, _, _ = _ctx(token="secret")
    status, _ = _call(
        ctx, "GET", "/api/folders", headers={"Authorization": "Bearer nope"}
    )
    assert status == 401


def test_no_token_disables_auth():
    ctx, _, _ = _ctx(token="")
    status, _ = _call(ctx, "GET", "/api/folders")
    assert status == 200


# ── Чтение ───────────────────────────────────────────────────────────────────


def test_folders():
    ctx, _, _ = _ctx()
    status, payload = _call(ctx, "GET", "/api/folders")
    assert status == 200
    assert payload["folders"][0]["folder_path"] == "Docs"


def test_files_passes_filters():
    ctx, repo, _ = _ctx()
    status, payload = _call(
        ctx,
        "GET",
        "/api/files",
        query={"folder": "Docs", "search": "a", "recursive": "1"},
    )
    assert status == 200
    assert payload["files"][0]["file_key"] == "k1"
    assert repo.unified_calls == [("Docs", "a", None, True)]


def test_jobs_and_job_by_id():
    ctx, _, _ = _ctx()
    status, payload = _call(ctx, "GET", "/api/jobs")
    assert status == 200
    assert payload["jobs"][0]["id"] == 7

    status, payload = _call(ctx, "GET", "/api/jobs/7")
    assert status == 200
    assert payload["job"]["id"] == 7

    status, payload = _call(ctx, "GET", "/api/jobs/999")
    assert status == 404


def test_unknown_endpoint_and_method():
    ctx, _, _ = _ctx()
    assert _call(ctx, "GET", "/api/nope")[0] == 404
    assert _call(ctx, "PUT", "/api/folders")[0] == 405


# ── Запись ───────────────────────────────────────────────────────────────────


def test_upload_validates_and_submits(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x", encoding="utf-8")
    ctx, _, worker = _ctx()

    # Пустой список → 400.
    assert _call(ctx, "POST", "/api/upload", body={"paths": []})[0] == 400
    # Несуществующий файл → 400.
    assert _call(ctx, "POST", "/api/upload", body={"paths": ["/no/such"]})[0] == 400

    status, payload = _call(
        ctx, "POST", "/api/upload", body={"paths": [str(f)], "folder": "Docs"}
    )
    assert status == 202 and payload["accepted"] is True
    job_type, sent = worker.submitted[-1]
    assert job_type == "upload"
    assert sent["folder_path"] == "Docs"
    assert sent["file_paths"] == [str(f)]


def test_download_submits():
    ctx, _, worker = _ctx()
    assert _call(ctx, "POST", "/api/download", body={"folder": "Docs"})[0] == 400
    status, _ = _call(
        ctx, "POST", "/api/download", body={"folder": "Docs", "file_key": "k1"}
    )
    assert status == 202
    job_type, sent = worker.submitted[-1]
    assert job_type == "download"
    assert sent == {"folder_path": "Docs", "file_key": "k1", "allow_incomplete": False}


def test_delete_submits():
    ctx, _, worker = _ctx()
    status, _ = _call(
        ctx, "POST", "/api/delete", body={"folder": "Docs", "file_key": "k1"}
    )
    assert status == 202
    assert worker.submitted[-1] == ("delete", {"folder_path": "Docs", "file_key": "k1"})


def test_worker_not_ready_returns_503():
    ctx, _, _ = _ctx(accept=False)
    status, payload = _call(
        ctx, "POST", "/api/download", body={"folder": "Docs", "file_key": "k1"}
    )
    assert status == 503
    assert "error" in payload


def test_bad_json_body_returns_400():
    ctx, _, _ = _ctx()
    status, _ = dispatch(ctx, "POST", "/api/upload", {}, {}, b"{not json")
    assert status == 400


# ── Сквозной HTTP (реальный сокет) ───────────────────────────────────────────


def test_real_http_roundtrip():
    repo = FakeRepo()
    worker = FakeWorker()
    config = SimpleNamespace(
        api=ApiConfig(enabled=True, host="127.0.0.1", port=0, token="tok")
    )
    server = ApiServer(config, repo, worker)
    assert server.start() is True
    try:
        host, port = server.address
        base = f"http://{host}:{port}"

        # health без токена
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["status"] == "ok"

        # без токена на защищённый эндпоинт → 401
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/folders", timeout=5)
        assert exc.value.code == 401

        # с токеном → 200 + данные
        req = urllib.request.Request(
            f"{base}/api/folders", headers={"Authorization": "Bearer tok"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["folders"][0]["folder_path"] == "Docs"

        # POST download
        req = urllib.request.Request(
            f"{base}/api/download",
            data=json.dumps({"folder": "Docs", "file_key": "k1"}).encode(),
            headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 202
        assert worker.submitted[-1][0] == "download"
    finally:
        server.stop()
    assert server.running is False


def test_disabled_server_does_not_start():
    config = SimpleNamespace(api=ApiConfig(enabled=False))
    server = ApiServer(config, FakeRepo(), FakeWorker())
    assert server.start() is False
    assert server.running is False
