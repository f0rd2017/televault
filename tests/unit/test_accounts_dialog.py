"""Smoke-тесты диалога аккаунтов: таблица + фоновая проверка живости.

Сетевые проверки (_probe_proxy / _probe_account) замоканы, чтобы не ждать
реальных коннектов к Telegram.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core.types import TelegramAccount
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.ui.dialogs._accounts import AccountsDialog, _StatusProbe


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
    # Не стартуем фоновый поток в этом тесте — проверяем только структуру.
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)
    try:
        assert dlg.table.columnCount() == 10
        assert dlg.table.horizontalHeaderItem(8).text() == "Статус прокси"
        assert dlg.table.horizontalHeaderItem(9).text() == "Статус акк"
        assert dlg.table.rowCount() == 2
        # Колонки статуса инициализированы плейсхолдером.
        assert dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text() == "—"
        # Канал — это кнопка-виджет, а не текстовая ячейка.
        from PySide6.QtWidgets import QPushButton

        ch_widget = dlg.table.cellWidget(1, AccountsDialog.COL_CHANNEL)
        assert isinstance(ch_widget, QPushButton)
        # Прокси показывается коротко (только host:port, без логина/пароля).
        assert dlg.table.item(1, AccountsDialog.COL_PROXY).text() == "1.2.3.4:1080"
    finally:
        dlg.deleteLater()


def test_short_proxy_and_proxy_guard() -> None:
    # Короткий прокси скрывает креды.
    assert AccountsDialog._short_proxy("1.2.3.4:1080:u:p") == "1.2.3.4:1080"
    assert AccountsDialog._short_proxy("") == "Без прокси"
    assert AccountsDialog._short_proxy("socks5://h:9050") == "h:9050"
    # Защита канала от прокси: канал НЕ прокси, прокси — прокси.
    assert AccountsDialog._looks_like_proxy("https://t.me/+4GlhQFIW3tQ5Mjdi") is False
    assert AccountsDialog._looks_like_proxy("@reyn_bow") is False
    assert AccountsDialog._looks_like_proxy("194.104.238.225:63819:u:p") is True


def test_probe_fills_status_cells(tmp_path, monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])

    # Мокаем сетевые проверки на мгновенные детерминированные ответы.
    monkeypatch.setattr(
        _StatusProbe,
        "_probe_proxy",
        staticmethod(
            lambda acc: (
                ("— прямое", None)
                if acc.is_primary
                else ("✅ работает", ("socks5", "1.2.3.4", 1080, True, "u", "p"))
            )
        ),
    )

    async def fake_probe_account(cls, acc, proxy_tuple):
        return "✅ онлайн @user" if acc.is_primary else "❌ недоступен"

    monkeypatch.setattr(_StatusProbe, "_probe_account", classmethod(fake_probe_account))

    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)  # _start_probe запускается автоматически

    # Прокачиваем event loop, пока поток не завершится (с таймаутом).
    import time

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        app.processEvents()
        if dlg._probe is None or not dlg._probe.isRunning():
            # дать сигналам row_status доставиться
            app.processEvents()
            break
        time.sleep(0.02)

    app.processEvents()

    try:
        proxy_primary = dlg.table.item(0, AccountsDialog.COL_PROXY_STATUS).text()
        acc_primary = dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text()
        proxy_secondary = dlg.table.item(1, AccountsDialog.COL_PROXY_STATUS).text()
        acc_secondary = dlg.table.item(1, AccountsDialog.COL_ACC_STATUS).text()

        assert proxy_primary == "— прямое"
        assert acc_primary.startswith("✅ онлайн")
        assert proxy_secondary == "✅ работает"
        assert acc_secondary == "❌ недоступен"
        # Кнопка снова активна после завершения.
        assert dlg.check_btn.isEnabled()
        # После завершения ссылка на поток сброшена в None.
        assert dlg._probe is None
    finally:
        dlg.deleteLater()


def test_close_after_probe_finishes_does_not_crash(tmp_path, monkeypatch) -> None:
    """Регрессия: RuntimeError 'C++ object _StatusProbe already deleted' в done()."""
    app = QApplication.instance() or QApplication([])

    monkeypatch.setattr(
        _StatusProbe,
        "_probe_proxy",
        staticmethod(lambda acc: ("— прямое", None)),
    )

    async def fake_probe_account(cls, acc, proxy_tuple):
        return "✅ онлайн"

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

    # Дать deleteLater реально уничтожить C++-объект потока.
    app.processEvents()
    app.sendPostedEvents(None, 0)  # тип 0 включает DeferredDelete
    app.processEvents()

    # Закрытие после завершения проверки не должно падать.
    dlg.done(0)
    assert dlg._probe is None


def test_status_maps_by_account_id_not_row(tmp_path, monkeypatch) -> None:
    """Результат проверки ложится на строку нужного аккаунта по ID, а не индексу."""
    monkeypatch.setattr(AccountsDialog, "_start_probe", lambda self: None)
    _ = QApplication.instance() or QApplication([])
    repo = _make_repo(tmp_path)
    dlg = AccountsDialog(repo)
    try:
        # ID второй строки.
        target_id = int(dlg.table.item(1, AccountsDialog.COL_ID).text())
        other_id = int(dlg.table.item(0, AccountsDialog.COL_ID).text())

        dlg._on_row_status(target_id, "✅ работает", "✅ онлайн")
        assert dlg.table.item(1, AccountsDialog.COL_ACC_STATUS).text() == "✅ онлайн"
        # Чужая строка не тронута.
        assert dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text() == "—"

        # Несуществующий ID — без падений и без эффекта.
        dlg._on_row_status(999999, "x", "y")
        assert dlg.table.item(0, AccountsDialog.COL_ACC_STATUS).text() == "—"
        assert other_id != target_id
    finally:
        dlg.deleteLater()


def test_session_disk_path_and_missing_session() -> None:
    import asyncio

    # Расширение .session добавляется, если его нет.
    assert _StatusProbe._session_disk_path("/x/a") == "/x/a.session"
    assert _StatusProbe._session_disk_path("/x/a.session") == "/x/a.session"

    # Реальная проверка на несуществующей сессии — без сети, копировать нечего.
    acc = TelegramAccount(
        id=1,
        label="ghost",
        session_path="/nonexistent/path/ghost.session",
        tg_api_id=1,
        tg_api_hash="h",
        chat_target="@x",
    )
    result = asyncio.run(_StatusProbe._probe_account(acc, None))
    assert result == "⚠️ нет сессии"
