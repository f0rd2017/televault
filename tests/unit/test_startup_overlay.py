from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from app.ui.widgets import StartupLoadingOverlay


# Keep parent widgets alive so their C++ objects (and child overlays) aren't GC'd.
_KEEP_ALIVE: list[QWidget] = []


def _overlay() -> StartupLoadingOverlay:
    QApplication.instance() or QApplication([])
    parent = QWidget()
    _KEEP_ALIVE.append(parent)
    return StartupLoadingOverlay(parent)


def test_loading_hides_action_buttons():
    ov = _overlay()
    ov.show_loading("Connecting…")
    assert ov._status.text() == "Connecting…"
    assert ov._buttons.isHidden()


def test_error_state_shows_buttons_and_message():
    ov = _overlay()
    ov.show_error("no accounts")
    assert "no accounts" in ov._status.text()
    assert not ov._buttons.isHidden()


def test_retry_and_accounts_signals_fire():
    ov = _overlay()
    ov.show_error("boom")
    fired = {"retry": 0, "accounts": 0}
    ov.retry_requested.connect(lambda: fired.__setitem__("retry", fired["retry"] + 1))
    ov.accounts_requested.connect(
        lambda: fired.__setitem__("accounts", fired["accounts"] + 1)
    )
    ov._retry_btn.click()
    ov._accounts_btn.click()
    assert fired == {"retry": 1, "accounts": 1}


def test_finish_hides_overlay():
    ov = _overlay()
    ov.show_loading()
    ov.finish()
    # finish() animates opacity to 0 then hides; force the animation to its end.
    ov._fade_anim.setCurrentTime(ov._fade_anim.duration())
    assert ov.isHidden()
