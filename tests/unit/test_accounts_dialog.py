"""Smoke tests for the accounts dialog: the table + background liveness probe.

Network checks (_probe_proxy / _probe_account) are mocked to avoid waiting on
real connections to Telegram.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from televault.core.types import TelegramAccount
from televault.db.database import connect_db
from televault.db.repo import DbRepo
from televault.ui.dialogs import ConfirmDialog
from televault.ui.dialogs._accounts import AccountsDialog, _StatusProbe


def _make_repo(tmp_path) -> DbRepo:
    repo = DbRepo(connect_db(tmp_path / "index.sqlite3"))
    repo.insert_account(
        TelegramAccount(
            id=0,
            label="a1",
            session_path=str(tmp_path / "a1.session"),
            tg_api_id=1,
            tg_api_hash="h",
            chat_target="https://t.me/+abc",
            is_primary=True,
            proxy="",
        )
    )
    repo.insert_account(
        TelegramAccount(
            id=0,
            label="a2",
            session_path=str(tmp_path / "a2.session"),
            tg_api_id=1,
            tg_api_hash="h",
            chat_target="https://t.me/+abc",
            is_primary=False,
            proxy="1.2.3.4:1080:u:p",
        )
    )
    return repo


def test_dialog_builds_status_columns(tmp_path, monkeypatch) -> None:
    # Don't start the background thread in this test — we only check the structure.
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)
    try:
        assert dlg.table.columnCount() == 10
        assert dlg.table.horizontalHeaderItem(8).text() == "Proxy status"
        assert dlg.table.horizontalHeaderItem(9).text() == "Account status"
        assert dlg.table.rowCount() == 2
        # Status columns are initialized with a placeholder.
        assert dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text() == "—"
        # The channel is a button widget, not a text cell.
        from PySide6.QtWidgets import QPushButton

        ch_widget = dlg.table.cellWidget(1, AccountsDialog.COL_CHANNEL)
        assert isinstance(ch_widget, QPushButton)
        # The proxy is shown briefly (host:port only, no login/password).
        assert dlg.table.item(1, AccountsDialog.COL_PROXY).text() == "1.2.3.4:1080"
    finally:
        dlg.deleteLater()


def test_short_proxy_and_proxy_guard() -> None:
    # A short proxy hides the credentials.
    assert AccountsDialog._short_proxy("1.2.3.4:1080:u:p") == "1.2.3.4:1080"
    assert AccountsDialog._short_proxy("") == "No proxy"
    assert AccountsDialog._short_proxy("socks5://h:9050") == "h:9050"
    # Guard the channel against the proxy: the channel is NOT a proxy, the proxy is.
    assert AccountsDialog._looks_like_proxy("https://t.me/+AbCdEfGh12345678") is False
    assert AccountsDialog._looks_like_proxy("@example_channel") is False
    assert AccountsDialog._looks_like_proxy("203.0.113.10:1080:u:p") is True


def test_probe_fills_status_cells(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])

    # Mock the network checks to instant deterministic responses.
    monkeypatch.setattr(
        _StatusProbe,
        "_probe_proxy",
        staticmethod(
            lambda acc: (
                ("— direct", None)
                if acc.is_primary
                else ("✅ works", ("socks5", "1.2.3.4", 1080, True, "u", "p"))
            )
        ),
    )

    async def fake_probe_account(cls, acc, proxy_tuple):
        return "✅ online @user" if acc.is_primary else "❌ unavailable"

    monkeypatch.setattr(_StatusProbe, "_probe_account", classmethod(fake_probe_account))

    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)  # _start_probe starts automatically

    # Pump the event loop until the thread finishes (with a timeout).
    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        app.processEvents()
        if dlg._probe is None or not dlg._probe.isRunning():
            # let the row_status signals be delivered
            app.processEvents()
            break
        time.sleep(0.02)

    app.processEvents()

    try:
        proxy_primary = dlg.table.item(0, AccountsDialog.COL_PROXY_STATUS).text()
        acc_primary = dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text()
        proxy_secondary = dlg.table.item(1, AccountsDialog.COL_PROXY_STATUS).text()
        acc_secondary = dlg.table.item(1, AccountsDialog.COL_ACC_STATUS).text()

        assert proxy_primary == "— direct"
        assert acc_primary.startswith("✅ online")
        assert proxy_secondary == "✅ works"
        assert acc_secondary == "❌ unavailable"
        # The button is active again after completion.
        assert dlg.check_btn.isEnabled()
        # After completion, the thread reference is reset to None.
        assert dlg._probe is None
    finally:
        dlg.deleteLater()


def test_close_after_probe_finishes_does_not_crash(tmp_path, monkeypatch) -> None:
    """Regression: RuntimeError 'C++ object _StatusProbe already deleted' in done()."""
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        _StatusProbe,
        "_probe_proxy",
        staticmethod(lambda acc: ("— direct", None)),
    )

    async def fake_probe_account(cls, acc, proxy_tuple):
        return "✅ online"

    monkeypatch.setattr(_StatusProbe, "_probe_account", classmethod(fake_probe_account))

    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)

    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        app.processEvents()
        if dlg._probe is None:
            break
        time.sleep(0.02)

    # Let deleteLater actually destroy the thread's C++ object.
    app.processEvents()
    app.sendPostedEvents(None, 0)  # type 0 includes DeferredDelete
    app.processEvents()

    # Closing after the probe finishes must not crash.
    dlg.done(0)
    assert dlg._probe is None


def test_status_maps_by_account_id_not_row(tmp_path, monkeypatch) -> None:
    """The probe result lands on the row of the right account by id, not by index."""
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)
    try:
        # The id of the second row.
        target_id = int(dlg.table.item(1, AccountsDialog.COL_ID).text())
        other_id = int(dlg.table.item(0, AccountsDialog.COL_ID).text())

        dlg._on_row_status(target_id, "✅ works", "✅ online")
        assert dlg.table.item(1, AccountsDialog.COL_ACC_STATUS).text() == "✅ online"
        # A different row is untouched.
        assert dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text() == "—"

        # A nonexistent id — no crash and no effect.
        dlg._on_row_status(999999, "x", "y")
        assert dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text() == "—"
        assert other_id != target_id
    finally:
        dlg.deleteLater()


def test_session_disk_path_and_missing_session() -> None:
    import asyncio

    # The .session extension is added if it's missing.
    assert _StatusProbe._session_disk_path("/x/a") == "/x/a.session"
    assert _StatusProbe._session_disk_path("/x/a.session") == "/x/a.session"

    # A real check on a nonexistent session — no network, nothing to copy.
    acc = TelegramAccount(
        id=1,
        label="ghost",
        session_path="/nonexistent/path/ghost.session",
        tg_api_id=1,
        tg_api_hash="h",
        chat_target="@x",
    )
    result = asyncio.run(_StatusProbe._probe_account(acc, None))
    assert result == "⚠️ no session"


def test_is_saved_messages_detection() -> None:
    assert AccountsDialog._is_saved_messages("me") is True
    assert AccountsDialog._is_saved_messages("Me") is True
    assert AccountsDialog._is_saved_messages(" self ") is True
    assert AccountsDialog._is_saved_messages("@channel") is False
    assert AccountsDialog._is_saved_messages("") is False


def test_channel_button_shows_saved_messages_label(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    repo = _make_repo(tmp_path)
    acc_id = repo.list_accounts()[0].id
    repo.update_account(acc_id, chat_target="me")
    dlg = AccountsDialog(repo)
    try:
        widget = dlg.table.cellWidget(0, AccountsDialog.COL_CHANNEL)
        assert widget.text() == "Saved Messages"
        other_widget = dlg.table.cellWidget(1, AccountsDialog.COL_CHANNEL)
        assert other_widget.text() == "Link"
    finally:
        dlg.deleteLater()


def test_copy_channel_saved_messages_shows_info_not_clipboard(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    info_calls = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: info_calls.append(a))
    )
    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)
    try:
        dlg._copy_channel("me")
        assert len(info_calls) == 1
    finally:
        dlg.deleteLater()


def test_use_saved_messages_switches_chat_target(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        ConfirmDialog, "exec", lambda *_a, **_k: QDialog.DialogCode.Accepted
    )
    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)
    try:
        acc_id = dlg._accounts[0].id
        dlg.table.setCurrentCell(0, 0)
        dlg._on_use_saved_messages()
        updated = repo.get_account(acc_id)
        assert updated.chat_target == "me"
    finally:
        dlg.deleteLater()


def test_use_saved_messages_already_set_skips_confirm(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    info_calls = []
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: info_calls.append(a))
    )
    confirm_calls = []
    monkeypatch.setattr(
        ConfirmDialog,
        "exec",
        lambda *_a, **_k: confirm_calls.append(1) or QDialog.DialogCode.Accepted,
    )
    repo = _make_repo(tmp_path)
    acc_id = repo.list_accounts()[0].id
    repo.update_account(acc_id, chat_target="me")
    dlg = AccountsDialog(repo)
    try:
        dlg.table.setCurrentCell(0, 0)
        dlg._on_use_saved_messages()
        assert confirm_calls == []
        assert len(info_calls) == 1
    finally:
        dlg.deleteLater()
