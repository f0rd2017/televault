from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.api.server import ApiContext, ApiServer, FileResponse, dispatch
from app.core.sharing import (
    hash_share_password,
    new_share_token,
    verify_share_password,
)
from app.core.types import ApiConfig
from app.db.database import connect_db
from app.db.repo import DbRepo

CONTENT = b"hello-share-content-0123456789-abcdefghij"


# ── Sharing helpers (pure) ───────────────────────────────────────────────────


def test_token_is_random_and_urlsafe():
    a, b = new_share_token(), new_share_token()
    assert a != b and len(a) >= 16
    assert all(c.isalnum() or c in "-_" for c in a)


def test_password_hash_roundtrip():
    h = hash_share_password("s3cret")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_share_password("s3cret", h)
    assert not verify_share_password("nope", h)


def test_empty_password_means_no_password():
    assert hash_share_password("") == ""
    assert verify_share_password("anything", "") is True
    assert verify_share_password("", hash_share_password("x")) is False
    assert verify_share_password("x", "garbage$bad") is False


# ── Repo ─────────────────────────────────────────────────────────────────────


def _repo(tmp_path) -> DbRepo:
    return DbRepo(connect_db(tmp_path / "idx.sqlite3"))


def test_repo_share_crud(tmp_path):
    repo = _repo(tmp_path)
    tok = repo.create_share(
        "tok1", "Docs", "k1", "a.pdf", total_size=100, password_hash="ph", expires_ts=0
    )
    assert tok == "tok1"
    s = repo.get_share("tok1")
    assert s["orig_name"] == "a.pdf" and s["has_password"] is True
    assert repo.get_share("nope") is None
    assert len(repo.list_shares()) == 1
    repo.increment_share_downloads("tok1")
    assert repo.get_share("tok1")["download_count"] == 1
    assert repo.revoke_share("tok1") == 1
    assert repo.get_share("tok1")["revoked"] is True
    assert repo.delete_share("tok1") == 1
    assert repo.get_share("tok1") is None


# ── Worker stub + context ────────────────────────────────────────────────────


class FakeWorker:
    def __init__(self, content: bytes = CONTENT) -> None:
        self.content = content
        self.calls: list[tuple] = []

    def assemble_file_blocking(
        self, folder, file_key, dest_dir, timeout: float = 1800.0
    ) -> str:
        self.calls.append((folder, file_key, dest_dir))
        d = Path(dest_dir)
        d.mkdir(parents=True, exist_ok=True)
        f = d / "assembled.bin"
        f.write_bytes(self.content)
        return str(f)


def _ctx(tmp_path, *, token: str = "", worker: FakeWorker | None = None):
    repo = _repo(tmp_path)
    worker = worker or FakeWorker()
    config = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        download_root=str(tmp_path / "dl"),  # empty → forces assembly via the worker
        api=ApiConfig(enabled=True, host="127.0.0.1", port=20451, token=token),
    )
    ctx = ApiContext(
        repo=repo,
        worker=worker,
        token=token,
        config=config,
        share_dir=str(tmp_path / "share"),
    )
    return ctx, repo, worker


def _call(ctx, method, path, *, query=None, headers=None, body=None):
    q = {k: [v] for k, v in (query or {}).items()}
    raw = json.dumps(body).encode() if body is not None else b""
    return dispatch(ctx, method, path, q, headers or {}, raw)


# ── Share management via dispatch ────────────────────────────────────────────


def test_create_list_revoke_delete_share(tmp_path):
    ctx, repo, _ = _ctx(tmp_path)
    status, payload = _call(
        ctx,
        "POST",
        "/api/shares",
        body={"folder": "Docs", "file_key": "k1", "orig_name": "a.pdf"},
    )
    assert status == 201
    token = payload["token"]
    assert payload["url"].endswith(f"/share/{token}")

    status, payload = _call(ctx, "GET", "/api/shares")
    assert status == 200 and len(payload["shares"]) == 1
    assert "password_hash" not in payload["shares"][0]  # the secret does not leak

    status, _ = _call(ctx, "POST", f"/api/shares/{token}/revoke")
    assert status == 200 and repo.get_share(token)["revoked"] is True

    status, _ = _call(ctx, "DELETE", f"/api/shares/{token}")
    assert status == 200 and repo.get_share(token) is None

    assert _call(ctx, "DELETE", "/api/shares/missing")[0] == 404


def test_create_share_requires_fields(tmp_path):
    ctx, _, _ = _ctx(tmp_path)
    assert _call(ctx, "POST", "/api/shares", body={"folder": "Docs"})[0] == 400


# ── Serving: token/expiry/password checks ────────────────────────────────────


def test_serve_unknown_revoked_expired(tmp_path):
    ctx, repo, _ = _ctx(tmp_path)
    assert _call(ctx, "GET", "/share/nope")[0] == 404

    repo.create_share("rev", "Docs", "k1", "a.bin")
    repo.revoke_share("rev")
    assert _call(ctx, "GET", "/share/rev")[0] == 410

    repo.create_share("exp", "Docs", "k1", "a.bin", expires_ts=int(time.time()) - 5)
    assert _call(ctx, "GET", "/share/exp")[0] == 410


def test_serve_password_protected(tmp_path):
    ctx, repo, worker = _ctx(tmp_path)
    repo.create_share(
        "pw", "Docs", "k1", "a.bin", password_hash=hash_share_password("open")
    )
    # without a password → 401
    assert _call(ctx, "GET", "/share/pw")[0] == 401
    # wrong → 401
    assert _call(ctx, "GET", "/share/pw", query={"pw": "bad"})[0] == 401
    # correct → FileResponse
    result = _call(ctx, "GET", "/share/pw", query={"pw": "open"})
    assert isinstance(result, FileResponse)
    assert Path(result.path).read_bytes() == CONTENT


def test_serve_assembles_and_counts(tmp_path):
    ctx, repo, worker = _ctx(tmp_path)
    repo.create_share("ok", "Docs", "k1", "movie.mp4")
    result = _call(ctx, "GET", "/share/ok")
    assert isinstance(result, FileResponse)
    assert result.content_type == "video/mp4"
    assert worker.calls  # assembly was invoked (no local file)
    assert repo.get_share("ok")["download_count"] == 1


def test_serve_prefers_local_file(tmp_path):
    ctx, repo, worker = _ctx(tmp_path)
    # Put an 'already downloaded' file in download_root → the worker must not be called.
    local = Path(ctx.config.download_root) / "Docs" / "doc.bin"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"local-bytes")
    repo.create_share("loc", "Docs", "k1", "doc.bin", total_size=len(b"local-bytes"))
    result = _call(ctx, "GET", "/share/loc")
    assert isinstance(result, FileResponse)
    assert Path(result.path) == local
    assert worker.calls == []  # local file — no reassembly


# ── End-to-end HTTP with Range (real socket) ─────────────────────────────────


def test_real_http_share_download_and_range(tmp_path):
    repo = _repo(tmp_path)
    worker = FakeWorker()
    config = SimpleNamespace(
        cache_dir=str(tmp_path / "cache"),
        download_root=str(tmp_path / "dl"),
        api=ApiConfig(enabled=True, host="127.0.0.1", port=0, token="adm"),
    )
    server = ApiServer(config, repo, worker)
    assert server.start() is True
    try:
        host, port = server.address
        base = f"http://{host}:{port}"
        repo.create_share("pub", "Docs", "k1", "data.bin")

        # Public serving without an API token.
        with urllib.request.urlopen(f"{base}/share/pub", timeout=5) as resp:
            assert resp.status == 200
            assert resp.read() == CONTENT
            assert resp.headers.get("Accept-Ranges") == "bytes"

        # Range request → 206 + slice.
        req = urllib.request.Request(
            f"{base}/share/pub", headers={"Range": "bytes=0-4"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 206
            assert resp.read() == CONTENT[:5]
            assert resp.headers.get("Content-Range") == f"bytes 0-4/{len(CONTENT)}"

        # download_count increased (2 successful serves).
        assert repo.get_share("pub")["download_count"] == 2
    finally:
        server.stop()


# ── UI dialog ────────────────────────────────────────────────────────────────


def test_share_link_dialog_creates_share(tmp_path):
    from PySide6.QtWidgets import QApplication

    from app.core.types import ObjectEntry
    from app.ui.dialogs._properties import ShareLinkDialog

    QApplication.instance() or QApplication([])
    repo = _repo(tmp_path)
    entry = ObjectEntry(
        file_key="k1",
        folder_path="Docs",
        orig_name="a.pdf",
        parts_total=1,
        have_parts=1,
        status="complete",
        total_size=100,
        last_seen_ts=0,
    )
    config = SimpleNamespace(
        api=ApiConfig(enabled=True, host="127.0.0.1", port=20451, token="")
    )
    dlg = ShareLinkDialog(entry=entry, repo=repo, config=config)
    dlg._password_edit.setText("pw")
    dlg._expiry_combo.setCurrentIndex(2)  # 1 day
    dlg._on_create()

    url = dlg._url_edit.text()
    assert url.startswith("http://127.0.0.1:20451/share/")
    assert dlg._copy_btn.isEnabled()
    shares = repo.list_shares()
    assert len(shares) == 1
    assert shares[0]["has_password"] is True
    assert shares[0]["expires_ts"] > 0
