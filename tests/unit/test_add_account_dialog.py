"""Smoke tests for the GUI add-account dialog (no network: the auth worker
is never started; only form building and validation are exercised)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from app.core.types import TelegramAccount
from app.ui.dialogs._add_account import AddAccountDialog


class _FakeRepo:
    def __init__(self, accounts: list[TelegramAccount] | None = None):
        self._accounts = accounts or []

    def list_accounts(self):
        return list(self._accounts)


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _mute_warnings(monkeypatch):
    # QMessageBox.warning is modal and would hang the offscreen test run.
    from app.ui.dialogs import _add_account

    monkeypatch.setattr(
        _add_account.QMessageBox, "warning", staticmethod(lambda *a, **k: None)
    )


def _account(phone_masked: str = "7999123****", label: str = "Acc1") -> TelegramAccount:
    return TelegramAccount(
        id=1,
        label=label,
        session_path="var/data/account_sessions/acc_x.session",
        tg_api_id=1,
        tg_api_hash="h",
        chat_target="@chan",
        phone_masked=phone_masked,
    )


def test_dialog_builds_and_prefills_api_credentials(app):
    dlg = AddAccountDialog(_FakeRepo(), default_api_id=12345, default_api_hash="abc")
    assert dlg.windowTitle() == "Add Telegram Account"
    assert dlg.api_id_edit.text() == "12345"
    assert dlg.api_hash_edit.text() == "abc"


def test_validation_rejects_empty_and_bad_input(app):
    dlg = AddAccountDialog(_FakeRepo())
    assert dlg._validated_form() is None  # everything empty

    dlg.label_edit.setText("Acc")
    dlg.phone_edit.setText("not-a-phone")
    assert dlg._validated_form() is None  # bad phone

    dlg.phone_edit.setText("+79991234567")
    dlg.api_id_edit.setText("0")
    assert dlg._validated_form() is None  # bad api id

    dlg.api_id_edit.setText("12345")
    dlg.api_hash_edit.setText("hash")
    dlg.channel_edit.setText("::bad channel::")
    assert dlg._validated_form() is None  # bad channel

    dlg.channel_edit.setText("@mychannel")
    dlg.proxy_edit.setText("definitely not a proxy !!!")
    assert dlg._validated_form() is None  # bad proxy


def test_validation_accepts_good_form(app):
    dlg = AddAccountDialog(_FakeRepo())
    dlg.label_edit.setText("Acc 2")
    dlg.phone_edit.setText("+7 999 123-45-67")
    dlg.api_id_edit.setText("12345")
    dlg.api_hash_edit.setText("hash")
    dlg.channel_edit.setText("https://t.me/+AbCdEf123")

    form = dlg._validated_form()
    assert form is not None
    assert form["phone"] == "+79991234567"
    assert form["api_id"] == 12345
    assert form["channel"] == "https://t.me/+AbCdEf123"
    assert form["proxy"] == ""


def test_validation_detects_duplicate_phone(app):
    # phone_masked keeps the number without its last 4 digits.
    repo = _FakeRepo([_account(phone_masked="7999123****")])
    dlg = AddAccountDialog(repo)
    dlg.label_edit.setText("Acc 2")
    dlg.phone_edit.setText("+79991234567")
    dlg.api_id_edit.setText("12345")
    dlg.api_hash_edit.setText("hash")
    dlg.channel_edit.setText("@mychannel")
    assert dlg._validated_form() is None

    # A different number passes.
    dlg.phone_edit.setText("+380501112233")
    assert dlg._validated_form() is not None


def test_saved_messages_checkbox_uses_me_as_channel(app):
    dlg = AddAccountDialog(_FakeRepo())
    dlg.label_edit.setText("Acc 2")
    dlg.phone_edit.setText("+79991234567")
    dlg.api_id_edit.setText("12345")
    dlg.api_hash_edit.setText("hash")
    dlg.saved_messages_check.setChecked(True)

    assert not dlg.channel_edit.isEnabled()
    form = dlg._validated_form()
    assert form is not None
    assert form["channel"] == "me"


def test_saved_messages_checkbox_toggle_restores_channel_field(app):
    dlg = AddAccountDialog(_FakeRepo())
    assert dlg.channel_edit.isEnabled()
    dlg.saved_messages_check.setChecked(True)
    assert not dlg.channel_edit.isEnabled()
    dlg.saved_messages_check.setChecked(False)
    assert dlg.channel_edit.isEnabled()


def test_set_form_enabled_keeps_channel_disabled_when_saved_messages_checked(app):
    dlg = AddAccountDialog(_FakeRepo())
    dlg.saved_messages_check.setChecked(True)
    dlg._set_form_enabled(False)
    assert not dlg.channel_edit.isEnabled()
    assert not dlg.saved_messages_check.isEnabled()

    dlg._set_form_enabled(True)
    # Re-enabling the form must not re-enable the channel field, since
    # Saved Messages is still checked.
    assert not dlg.channel_edit.isEnabled()
    assert dlg.saved_messages_check.isEnabled()
