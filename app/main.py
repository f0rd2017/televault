from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app.api import ApiServer
from app.config.config import (
    ConfigError,
    config_exists,
    default_config_path,
    load_app_config,
    load_public_config,
    save_public_config,
)
from app.core.logging import setup_logging
from app.core.utils import app_icon_path, ensure_dir, ensure_parent_dir
from app.core.worker import TelegramWorker
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.tg.client import ensure_session_authorized
from app.ui.theme import apply_theme
from app.ui.dialogs import SetupDialog
from app.ui.window_main import MainWindow

logger = logging.getLogger(__name__)


def _database_path_from_session(session_path: str) -> Path:
    base = Path(session_path).expanduser().resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base / "index.sqlite3"


def _show_error(text: str) -> None:
    logger.error(text)
    print(text, file=sys.stderr)
    QMessageBox.critical(None, "Startup error", text)


def run() -> int:
    debug_enabled = (
        str(os.getenv("TELEVAULT_DEBUG", "")).strip().lower()
        in {"1", "true", "yes", "on"}
        or "--debug" in sys.argv
    )
    setup_logging(debug=debug_enabled)

    from telethon.crypto import aes as _tg_aes

    if _tg_aes.cryptg is not None:
        logger.info("MTProto AES backend: cryptg (fast native encryption)")
    else:
        logger.warning(
            "MTProto AES backend: cryptg is missing — transfers will be slow. "
            "Install it with: uv add cryptg"
        )

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("TeleVault")

    from PySide6.QtCore import QTranslator

    from app.core.i18n import i18n_dir, install_language, saved_language

    translator = QTranslator(app)
    lang = saved_language()
    install_language(app, lang, translator)
    app.tg_translator = translator  # kept around so the UI can retranslate on the fly
    app.i18n_path = i18n_dir()

    apply_theme(app)
    icon_path = app_icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    # Not cwd: a frozen build is launched from an arbitrary directory — look
    # for config.json next to the exe / at the project root (default_config_path).
    config_path = default_config_path()

    if not config_exists(config_path):
        setup_dialog = SetupDialog(initial=load_public_config(config_path))
        if setup_dialog.exec() != QDialog.DialogCode.Accepted:
            return 0
        save_public_config(setup_dialog.to_public_config(), config_path)

    try:
        config = load_app_config(config_path=config_path)
    except ConfigError as exc:
        _show_error(str(exc))
        return 1

    # Logging was already set up at the very start of run() (var/logs/televault.log,
    # with rotation). This used to call setup_logging(..., cache_dir/televault.log)
    # a second time here "to keep logs next to the data" — but setup_logging
    # fully replaces the handlers, so instead of moving the log it produced
    # TWO files: the old one (var/logs/) with a couple of lines before the
    # reconfiguration, orphaned on every run, and a new one (var/cache/).
    # cache_dir is for cache, not logs, so we simply don't reopen the log a
    # second time.
    # Log the accounts loaded from the DB.
    db_path_for_accounts = _database_path_from_session(config.tg_session_path)
    conn_for_accounts = connect_db(db_path_for_accounts)
    try:
        temp_repo = DbRepo(conn_for_accounts)
        accounts_for_log = temp_repo.list_accounts()
    finally:
        conn_for_accounts.close()

    account_log = (
        ", ".join(f"{a.label}({a.chat_target})" for a in accounts_for_log)
        if accounts_for_log
        else "none"
    )
    logger.info(
        (
            "Runtime config: accounts=[%s] sharding=%s "
            "chunk_size_mb=%d concurrency=%d max_active_jobs=%d "
            "integrity=%s compression=%s balanced_parts=%s regular_part_mb=%d premium_part_mb=%d "
            "small_batching=%s small_threshold_kb=%d small_batch_target_mb=%d small_parallel_jobs=%d"
        ),
        account_log,
        config.channel_sharding_mode,
        config.chunk_size_mb,
        config.concurrency,
        config.max_active_jobs,
        config.download_integrity_mode,
        config.upload_compression_mode,
        config.balanced_part_sizing_enabled,
        config.balanced_part_target_regular_mb,
        config.balanced_part_target_premium_mb,
        config.small_file_batching_enabled,
        config.small_file_threshold_kb,
        config.small_file_batch_target_mb,
        config.small_upload_parallel_jobs,
    )

    # Try to authorize main session, but don't block startup if it fails.
    # User accounts handle their own auth, so main session is optional for upload.
    try:
        asyncio.run(ensure_session_authorized(config, interactive=False))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Main session not authorized — scan/delete/reconcile will use user accounts. "
            "To authorize main session, run: python scripts/auth_session.py"
            " Error details: %s",
            str(e),
        )

    ensure_parent_dir(config.tg_session_path)
    ensure_dir(config.cache_dir)

    db_path = _database_path_from_session(config.tg_session_path)
    conn = connect_db(db_path)
    repo = DbRepo(conn)
    repo.init_schema()

    worker = TelegramWorker(config, repo)

    def save_config_callback(public_config: dict) -> None:
        save_public_config(public_config, config_path)

    window = MainWindow(
        config=config,
        repo=repo,
        worker=worker,
        save_config_callback=save_config_callback,
    )
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()
    worker.start()

    # Local REST API — disabled by default, see config.api.
    api_server = ApiServer(config, repo, worker)
    window.api_server = api_server
    try:
        api_server.start()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to start REST API")

    exit_code = app.exec()

    api_server.stop()
    worker.request_stop()
    worker.wait(10_000)
    conn.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
