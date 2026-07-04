from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTabWidget

from app.ui.dialogs import SettingsDialog, SetupDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _initial() -> dict:
    return {
        "download_integrity_mode": "fast",
        "upload_compression_mode": "force",
        "chunk_size_mb": 128,
        "cache_max_size_mb": 2048,
        "small_file_threshold_kb": 4096,
        "small_file_batch_target_mb": 32,
        "send_media_rate_limit": 9.5,
        "get_file_rate_limit": 18.0,
        "upload_limit_safety_mb": 64,
        "retry": {"max_attempts": 8, "base_delay": 2.5},
        "crypto": {"enabled": True, "key_env": "MYKEY"},
        "api": {"enabled": True, "host": "127.0.0.1", "port": 20451, "token": "tk"},
    }


def test_two_tabs_present():
    _app()
    dlg = SetupDialog(initial=_initial())
    tabs = dlg.findChild(QTabWidget)
    assert tabs is not None
    labels = [tabs.tabText(i) for i in range(tabs.count())]
    assert labels == ["Basic", "Advanced"]


def test_advanced_widgets_reflect_initial():
    _app()
    dlg = SetupDialog(initial=_initial())
    assert dlg.integrity_combo.currentData() == "fast"
    assert dlg.compression_combo.currentData() == "force"
    assert dlg.chunk_size_spin.value() == 128
    assert dlg.cache_limit_spin.value() == 2048
    assert dlg.small_threshold_spin.value() == 4096
    assert dlg.retry_attempts_spin.value() == 8
    assert abs(dlg.retry_delay_spin.value() - 2.5) < 1e-6
    assert dlg.crypto_enabled_chk.isChecked() is True
    assert dlg.crypto_key_env_edit.text() == "MYKEY"


def test_advanced_edits_flow_into_public_config():
    _app()
    dlg = SetupDialog(initial=_initial())
    # Поменять значения на «Расширенных» и убедиться, что они попадают в конфиг.
    dlg.integrity_combo.setCurrentIndex(dlg.integrity_combo.findData("strict"))
    dlg.compression_combo.setCurrentIndex(dlg.compression_combo.findData("off"))
    dlg.chunk_size_spin.setValue(256)
    dlg.send_rate_spin.setValue(7.0)
    dlg.retry_attempts_spin.setValue(10)
    dlg.crypto_enabled_chk.setChecked(False)

    cfg = dlg.to_public_config()
    assert cfg["download_integrity_mode"] == "strict"
    assert cfg["upload_compression_mode"] == "off"
    assert cfg["chunk_size_mb"] == 256
    assert cfg["send_media_rate_limit"] == 7.0
    assert cfg["retry"]["max_attempts"] == 10
    assert cfg["crypto"]["enabled"] is False


def test_crypto_key_env_disabled_when_unchecked():
    _app()
    initial = _initial()
    initial["crypto"] = {"enabled": False, "key_env": "TG_CRYPTO_KEY_B64"}
    dlg = SetupDialog(initial=initial)
    assert dlg.crypto_key_env_edit.isEnabled() is False
    dlg.crypto_enabled_chk.setChecked(True)
    assert dlg.crypto_key_env_edit.isEnabled() is True


def test_empty_key_env_falls_back_to_default():
    _app()
    dlg = SetupDialog(initial=_initial())
    dlg.crypto_key_env_edit.setText("   ")
    cfg = dlg.to_public_config()
    assert cfg["crypto"]["key_env"] == "TG_CRYPTO_KEY_B64"


def test_settings_dialog_title():
    _app()
    dlg = SettingsDialog(initial=_initial())
    assert dlg.windowTitle() == "Settings"
