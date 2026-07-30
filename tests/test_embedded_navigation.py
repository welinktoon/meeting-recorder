"""Regression tests for the single-window sidebar navigation."""

import unittest

from PyQt6.QtWidgets import QApplication, QWidget

from ui_qt.main_window import MainWindow


class TestEmbeddedSidebarNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow()
        self.workspace = self.window.voice_notes_workspace

    def tearDown(self):
        self.window._force_quit = True
        self.window.close()

    def _connect_page(self, signal, key):
        page = QWidget()
        page.setObjectName(f"{key}Page")
        signal.connect(
            lambda: self.workspace.set_embedded_page(key, page)
        )
        return page

    def test_sidebar_pages_replace_content_in_the_same_window(self):
        devices = self._connect_page(
            self.window.devices_requested, "devices"
        )
        models = self._connect_page(
            self.window.model_manager_requested, "models"
        )
        settings = self._connect_page(
            self.window.settings_requested, "settings"
        )

        for button, key, page in (
            (self.workspace.devices_button, "devices", devices),
            (self.workspace.models_button, "models", models),
            (self.workspace.settings_button, "settings", settings),
        ):
            button.click()
            self.app.processEvents()
            self.assertIs(self.workspace.content_stack.currentWidget(), page)
            self.assertFalse(page.isWindow())
            self.assertTrue(button.property("active"))

        self.workspace.records_button.click()
        self.app.processEvents()
        self.assertEqual(
            self.workspace.content_stack.currentWidget().objectName(),
            "recordsPage",
        )
        self.assertTrue(self.workspace.records_button.property("active"))

    def test_meeting_header_actions_are_accessible_icon_buttons(self):
        actions = (
            (
                self.workspace.open_media_button,
                "Открыть запись встречи",
            ),
            (
                self.workspace.codex_improve_button,
                "Улучшить расшифровку через Codex",
            ),
            (
                self.workspace.trash_button,
                "Переместить встречу в корзину",
            ),
        )

        for button, accessible_name in actions:
            self.assertEqual(button.text(), "")
            self.assertEqual(button.size().width(), 40)
            self.assertEqual(button.size().height(), 40)
            self.assertEqual(button.iconSize().width(), 17)
            self.assertFalse(button.icon().isNull())
            self.assertEqual(button.accessibleName(), accessible_name)


if __name__ == "__main__":
    unittest.main()
