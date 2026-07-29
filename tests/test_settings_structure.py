"""Regression tests for the simplified, task-oriented settings layout."""

from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from ui_qt.dialogs.settings_dialog import SettingsDialog


APP = QApplication.instance() or QApplication([])


def test_settings_are_grouped_by_user_task():
    dialog = SettingsDialog()

    assert [
        dialog.tabs.tabText(index)
        for index in range(dialog.tabs.count())
    ] == [
        "Запись",
        "Расшифровка",
        "Обработка текста",
        "Приложение",
    ]
    assert dialog._recording_tab_index == 0
    assert dialog._transcription_tab_index == 1
    assert dialog._application_tab_index == 3
    dialog.close()


def test_nonfunctional_duplicate_controls_are_not_exposed():
    dialog = SettingsDialog()

    assert not hasattr(dialog, "max_size_spinbox")
    assert not hasattr(dialog, "logging_check")
    assert dialog.check_updates_button.text() == "Проверить обновления"
    dialog.close()


def test_update_check_button_emits_request():
    dialog = SettingsDialog()
    requests = QSignalSpy(dialog.check_updates_requested)

    dialog.check_updates_button.click()

    assert len(requests) == 1
    dialog.close()
