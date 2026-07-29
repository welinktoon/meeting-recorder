"""Tests for the local, cancellable Codex transcript cleanup integration."""

import threading
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest

from services.codex_cleanup import (
    CodexCleanupMode,
    CodexConnectionStatus,
    CodexTranscriptCleanup,
    _codex_command,
    build_codex_prompt,
    start_codex_setup,
)
from ui_qt.dialogs.settings_dialog import SettingsDialog


class _CompletedProcess:
    returncode = 0

    def __init__(self, stdout="Исправленный текст.", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self._done = False
        self.received_input = ""

    def communicate(self, input="", timeout=None):
        self.received_input = input
        self._done = True
        return self.stdout, self.stderr

    def poll(self):
        return 0 if self._done else None

    def terminate(self):
        self._done = True


class _BlockingProcess(_CompletedProcess):
    def __init__(self):
        super().__init__(stdout="")
        self.started = threading.Event()
        self.terminated = threading.Event()
        self.returncode = None

    def communicate(self, input="", timeout=None):
        self.received_input = input
        self.started.set()
        self.terminated.wait(timeout=2)
        self.returncode = -15
        self._done = True
        return "", "terminated"

    def terminate(self):
        self.terminated.set()


def test_codex_prompt_treats_transcript_as_data_and_supports_modes():
    correct = build_codex_prompt(CodexCleanupMode.CORRECT)
    structured = build_codex_prompt(CodexCleanupMode.STRUCTURE)
    summary = build_codex_prompt(CodexCleanupMode.SUMMARY)

    assert "недоверенными данными" in correct
    assert "Не сокращай" in correct
    assert "«Темы обсуждения»" in structured
    assert "«Что решили»" in structured
    assert "«Полная расшифровка»" in structured
    assert "без сокращений и пропусков" in structured
    assert "Не возвращай расшифровку" in summary
    assert "«Итоги обсуждения»" in summary


def test_codex_cleanup_returns_only_cli_final_output():
    process = _CompletedProcess()
    status = CodexConnectionStatus(
        "ready",
        "Codex подключён",
        r"C:\Codex\codex.exe",
    )
    cleaner = CodexTranscriptCleanup()

    with patch(
        "services.codex_cleanup.get_codex_status",
        return_value=status,
    ), patch(
        "services.codex_cleanup.subprocess.Popen",
        return_value=process,
    ) as popen:
        result = cleaner.cleanup(
            "текст без точки",
            mode=CodexCleanupMode.CORRECT,
        )

    assert result == "Исправленный текст."
    assert process.received_input == "текст без точки"
    assert cleaner.last_error is None
    command = popen.call_args.args[0]
    assert "exec" in command
    assert "--ephemeral" in command
    assert "read-only" in command
    assert "--skip-git-repo-check" in command
    assert "--output-last-message" in command
    assert "--ask-for-approval" not in command


def test_codex_cleanup_falls_back_when_not_connected():
    cleaner = CodexTranscriptCleanup()
    status = CodexConnectionStatus(
        "not_logged_in",
        "Требуется вход в Codex",
    )

    with patch(
        "services.codex_cleanup.get_codex_status",
        return_value=status,
    ):
        result = cleaner.cleanup("исходный текст")

    assert result == "исходный текст"
    assert cleaner.last_error == "Требуется вход в Codex"


def test_windows_npm_shim_is_started_through_cmd():
    command = _codex_command(
        r"C:\Users\Test\AppData\Roaming\npm\codex.cmd",
        "--version",
    )

    assert command[1:4] == ["/d", "/s", "/c"]
    assert "codex.cmd" in command[4]
    assert "--version" in command[4]


def test_installed_npm_codex_shim_uses_node_to_preserve_unicode(tmp_path):
    shim = tmp_path / "codex.cmd"
    codex_js = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "bin"
        / "codex.js"
    )
    codex_js.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    codex_js.write_text("// entry", encoding="utf-8")

    with patch(
        "services.codex_cleanup.shutil.which",
        return_value=r"C:\Program Files\nodejs\node.exe",
    ):
        command = _codex_command(
            str(shim),
            "exec",
            "Длинная инструкция с пробелами",
        )

    assert command == [
        r"C:\Program Files\nodejs\node.exe",
        str(codex_js),
        "exec",
        "Длинная инструкция с пробелами",
    ]


def test_codex_setup_uses_official_npm_package_instead_of_install_ps1():
    with patch(
        "services.codex_cleanup.find_codex_cli",
        return_value=None,
    ), patch(
        "services.codex_cleanup.shutil.which",
        side_effect=lambda name: (
            r"C:\Program Files\nodejs\npm.cmd" if name == "npm.cmd" else None
        ),
    ), patch(
        "services.codex_cleanup.subprocess.Popen",
    ) as popen:
        start_codex_setup()

    powershell_script = popen.call_args.args[0][-1]
    assert "@openai/codex" in powershell_script
    assert "install.ps1" not in powershell_script


def test_codex_cleanup_can_be_canceled_without_losing_source_text():
    process = _BlockingProcess()
    status = CodexConnectionStatus(
        "ready",
        "Codex подключён",
        r"C:\Codex\codex.exe",
    )
    cleaner = CodexTranscriptCleanup()
    result = []

    with patch(
        "services.codex_cleanup.get_codex_status",
        return_value=status,
    ), patch(
        "services.codex_cleanup.subprocess.Popen",
        return_value=process,
    ):
        worker = threading.Thread(
            target=lambda: result.append(cleaner.cleanup("исходный текст"))
        )
        worker.start()
        assert process.started.wait(timeout=1)
        assert cleaner.is_running
        assert cleaner.cancel()
        worker.join(timeout=2)

    assert result == ["исходный текст"]
    assert cleaner.last_error == "canceled"
    assert not cleaner.is_running


def test_settings_expose_simple_codex_toggle_mode_and_connection():
    app = QApplication.instance() or QApplication([])
    status = CodexConnectionStatus(
        "ready",
        "Codex подключён",
        r"C:\Codex\codex.exe",
    )
    with patch(
        "ui_qt.dialogs.settings_dialog.get_codex_status",
        return_value=status,
    ), patch(
        "ui_qt.dialogs.settings_dialog.settings_manager.load_all_settings",
        return_value={},
    ), patch(
        "ui_qt.dialogs.settings_dialog.AudioRecorder.get_input_devices",
        return_value=[],
    ):
        dialog = SettingsDialog()
        QTest.qWait(100)
        app.processEvents()

    assert dialog.codex_cleanup_check.text() == (
        "Улучшать расшифровку через Codex"
    )
    assert [
        dialog.codex_mode_combo.itemText(index)
        for index in range(dialog.codex_mode_combo.count())
    ] == [
        "Исправить ошибки",
        "Темы, решения и расшифровка",
        "Только итоги и решения",
    ]
    assert dialog.codex_status_label.text() == "● Codex подключён"
    assert dialog.video_recording_enabled_check.isChecked()
    assert dialog.video_fps_spinbox.value() == 15
    assert dialog.video_quality_combo.currentData() == 24
    dialog.close()
