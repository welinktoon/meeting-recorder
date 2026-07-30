"""Three-column, note-centric screen for local recordings."""
from PyQt6.QtCore import Qt, pyqtSignal, QUrl, QTimer, QSize
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QPainter,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import QListWidgetItem
import math
import os
import re
import time
import wave
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QFrame, QTextEdit, QCheckBox,
    QListWidget, QStyle, QStackedWidget, QSizePolicy, QMessageBox)
import qtawesome as qta
from config import config
from services.history_manager import NO_SPEECH_TRANSCRIPT, history_manager
from services.hf_access import is_model_cached
from services.format_utils import format_file_size, format_timestamp
from services.settings import (
    settings_manager,
    SettingsKey,
    resolve_codex_cleanup_enabled,
)


class WaveformWidget(QWidget):
    """Clean continuous waveform instead of text-character blocks."""
    def __init__(self, parent=None):
        super().__init__(parent); self.setMinimumHeight(30); self.levels = [0.15] * 48; self.color = "#1769e0"
    def set_levels(self, levels):
        if levels:
            self.levels = list(levels)
            self.update()
    def set_color(self, color):
        self.color = color
        self.update()
    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = self.height() / 2; width = max(1, self.width() - 6)
        pen = QPen(QColor(self.color)); pen.setWidthF(1.6); painter.setPen(pen)
        count = max(1, len(self.levels))
        for x in range(3, int(width), 4):
            value = max(0.0, min(1.0, self.levels[int((x / width) * (count - 1))]))
            level = 2.5 + value * max(4, center - 3)
            painter.drawLine(x, int(center - level), x, int(center + level))


class ElidedLabel(QLabel):
    """Preserve the full title while drawing a clean trailing ellipsis."""

    def setText(self, text):
        value = str(text or "")
        super().setText(value)
        self.setToolTip(value)
        self.setAccessibleName(value)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().windowText().color())
        text = self.fontMetrics().elidedText(
            self.text(),
            Qt.TextElideMode.ElideRight,
            max(0, self.contentsRect().width()),
        )
        painter.drawText(
            self.contentsRect(),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text,
        )


class VoiceNotesWorkspace(QWidget):
    SORT_NEWEST = "newest"
    SORT_OLDEST = "oldest"
    SORT_SIZE = "size"
    SORT_DURATION = "duration"

    record_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    transcribe_requested = pyqtSignal(str)
    model_selected = pyqtSignal(str)
    theme_changed = pyqtSignal(str)
    settings_requested = pyqtSignal()
    devices_requested = pyqtSignal()
    models_requested = pyqtSignal()
    codex_improve_requested = pyqtSignal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("voiceNotesWorkspace")
        self.recording = False
        self.dark = False
        self._page_widgets = {}
        self._nav_buttons = {}
        self._selected_audio_path = ""
        self._selected_media_path = ""
        self._selected_history_id = ""
        self._selected_transcript_text = ""
        self._selected_enhanced_by_codex = False
        self._transcription_state = "idle"
        self._active_transcription_path = ""
        self._transcription_error = ""
        self._record_started_at = 0.0
        self._record_timer = QTimer(self)
        self._record_timer.setInterval(250)
        self._record_timer.timeout.connect(self._update_recording_timer)
        self._audio_output = QAudioOutput(self)
        self._media_player = QMediaPlayer(self)
        self._media_player.setAudioOutput(self._audio_output)
        self._media_player.positionChanged.connect(
            lambda position: self.elapsed_label.setText(self._format_time(position / 1000))
        )
        self._media_player.durationChanged.connect(
            lambda duration: self.duration_label.setText(self._format_time(duration / 1000))
        )
        self._media_player.playbackStateChanged.connect(self._update_play_button)
        self._build()
        self._apply_theme()
        self.refresh_history()

    def _icon(self, kind, color="#1769e0"):
        # One coherent Font Awesome solid icon family; never fall back to the
        # platform's mixed Qt stock icons.
        names = {
            "SP_FileIcon": "fa6s.video", "SP_MediaVolume": "fa6s.microphone",
            "SP_DriveHDIcon": "fa6s.brain", "SP_FileDialogDetailedView": "fa6s.gear",
            "SP_DesktopIcon": "fa6s.circle-half-stroke", "SP_FileDialogContentsView": "fa6s.sliders",
            "SP_TitleBarMenuButton": "fa6s.ellipsis", "SP_MediaPlay": "fa6s.play",
            "SP_DirOpenIcon": "fa6s.folder-open",
        }
        return qta.icon(names.get(kind.name, "fa6s.circle"), color=color)

    def _action_button(
        self,
        *,
        object_name,
        icon_name,
        label,
        tone="accent",
        callback=None,
    ):
        """Create a compact, accessible action using the shared icon family."""
        button = QPushButton()
        button.setObjectName(object_name)
        button.setProperty("iconName", icon_name)
        button.setProperty("iconTone", tone)
        button.setIconSize(QSize(17, 17))
        button.setFixedSize(40, 40)
        button.setToolTip(label)
        button.setAccessibleName(label)
        if callback:
            button.clicked.connect(callback)
        return button

    def _nav_button(self, label, icon, callback=None):
        button = QPushButton(label)
        button.setIcon(self._icon(icon))
        button.setProperty("iconKind", icon.name)
        button.setObjectName("navButton")
        button.setMinimumHeight(44)
        if callback:
            button.clicked.connect(callback)
        return button

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        nav = QFrame(); nav.setObjectName("nav"); nav.setFixedWidth(188)
        nav_layout = QVBoxLayout(nav); nav_layout.setContentsMargins(14, 22, 14, 16); nav_layout.setSpacing(8)
        self.records_button = self._nav_button("Встречи", QStyle.StandardPixmap.SP_FileIcon)
        self.records_button.clicked.connect(lambda: self.show_page("records"))
        self.records_button.setProperty("active", True); nav_layout.addWidget(self.records_button)
        self.devices_button = self._nav_button("Устройства", QStyle.StandardPixmap.SP_MediaVolume, self.devices_requested)
        self.models_button = self._nav_button("Модели", QStyle.StandardPixmap.SP_DriveHDIcon, self.models_requested)
        self.settings_button = self._nav_button("Настройки", QStyle.StandardPixmap.SP_FileDialogDetailedView, self.settings_requested)
        nav_layout.addWidget(self.devices_button)
        nav_layout.addWidget(self.models_button)
        nav_layout.addWidget(self.settings_button)
        self._nav_buttons = {
            "records": self.records_button,
            "devices": self.devices_button,
            "models": self.models_button,
            "settings": self.settings_button,
        }
        nav_layout.addStretch()
        self.theme_button = self._nav_button("", QStyle.StandardPixmap.SP_DesktopIcon, self.toggle_theme)
        self.theme_button.setObjectName("themeButton")
        self.theme_button.setFixedSize(42, 42)
        self.theme_button.setToolTip("Включить тёмную тему")
        self.theme_button.setAccessibleName("Включить тёмную тему")
        nav_layout.addWidget(
            self.theme_button,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        root.addWidget(nav)

        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("workspaceStack")
        records_page = QWidget()
        records_page.setObjectName("recordsPage")
        records_layout = QHBoxLayout(records_page)
        records_layout.setContentsMargins(0, 0, 0, 0)
        records_layout.setSpacing(0)

        listing = QFrame(); listing.setObjectName("list"); listing.setFixedWidth(388)
        list_layout = QVBoxLayout(listing); list_layout.setContentsMargins(24, 30, 20, 22); list_layout.setSpacing(16)
        header = QHBoxLayout(); title = QLabel("Все встречи"); title.setObjectName("sectionTitle"); header.addWidget(title); header.addStretch()
        list_layout.addLayout(header)
        self.search = QLineEdit(); self.search.setPlaceholderText("Поиск во встречах и тексте"); self.search.setAccessibleName("Поиск по названиям встреч и расшифровкам"); self.search.setToolTip("Искать в названиях встреч и текстах расшифровок"); self.search.setClearButtonEnabled(True); self.search.textChanged.connect(self._filter_notes); list_layout.addWidget(self.search)
        self.sort = QComboBox()
        self.sort.setAccessibleName("Сортировка встреч")
        self.sort.addItem("Сначала новые", self.SORT_NEWEST)
        self.sort.addItem("Сначала старые", self.SORT_OLDEST)
        self.sort.addItem("Сначала крупные", self.SORT_SIZE)
        self.sort.addItem("Сначала длинные", self.SORT_DURATION)
        self.sort.setToolTip("Сортировать встречи по дате, размеру файла или длительности")
        self.sort.currentIndexChanged.connect(self._sort_notes)
        list_layout.addWidget(self.sort)
        self.notes = QListWidget(); self.notes.setObjectName("notes")
        self.notes.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.notes.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.notes.setWordWrap(False)
        self.notes.currentItemChanged.connect(self._select_note)
        list_layout.addWidget(self.notes, 1); records_layout.addWidget(listing)

        main = QWidget(); main.setObjectName("main"); layout = QVBoxLayout(main); layout.setContentsMargins(40, 32, 48, 32); layout.setSpacing(20)
        top = QHBoxLayout(); top.setSpacing(8); self.note_name = ElidedLabel("Новая встреча"); self.note_name.setObjectName("noteName"); self.note_name.setMinimumWidth(0); self.note_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred); top.addWidget(self.note_name, 1)
        self.open_media_button = self._action_button(
            object_name="openMediaButton",
            icon_name="fa6s.arrow-up-right-from-square",
            label="Открыть запись встречи",
            callback=self._open_selected_media,
        )
        self.open_media_button.hide()
        top.addWidget(self.open_media_button)
        self.codex_improve_button = self._action_button(
            object_name="codexImproveButton",
            icon_name="fa6s.wand-magic-sparkles",
            label="Улучшить через Codex",
            callback=self._request_codex_improvement,
        )
        self.codex_improve_button.setToolTip(
            "Создать улучшенную версию без повторной расшифровки"
        )
        self.codex_improve_button.setAccessibleName(
            "Улучшить расшифровку через Codex"
        )
        self.codex_improve_button.hide()
        top.addWidget(self.codex_improve_button)
        self.trash_button = self._action_button(
            object_name="trashMeetingButton",
            icon_name="fa6s.trash-can",
            label="Переместить встречу в корзину",
            tone="danger",
            callback=self._move_selected_to_trash,
        )
        self.trash_button.setToolTip(
            "Переместить всю встречу и её расшифровки в корзину"
        )
        self.trash_button.hide()
        top.addWidget(self.trash_button)
        layout.addLayout(top)
        self.player = QFrame(); self.player.setObjectName("player"); player_layout = QHBoxLayout(self.player); player_layout.setContentsMargins(14, 12, 14, 12); player_layout.setSpacing(10)
        self.play_button = QPushButton(); self.play_button.setIcon(self._icon(QStyle.StandardPixmap.SP_MediaPlay)); self.play_button.setObjectName("playButton"); self.play_button.setAccessibleName("Воспроизвести"); self.play_button.clicked.connect(self._toggle_playback); player_layout.addWidget(self.play_button)
        self.elapsed_label = QLabel("00:00"); player_layout.addWidget(self.elapsed_label)
        self.waveform = WaveformWidget(); player_layout.addWidget(self.waveform, 1)
        self.duration_label = QLabel("00:00"); player_layout.addWidget(self.duration_label); layout.addWidget(self.player)
        self.source = QLabel("Выберите встречу слева"); self.source.setObjectName("source"); layout.addWidget(self.source)

        self.empty = QWidget(); self.empty.setObjectName("empty"); empty = QVBoxLayout(self.empty); empty.setAlignment(Qt.AlignmentFlag.AlignCenter); empty.setSpacing(14)
        self.empty_icon = QLabel()
        self.empty_icon.setPixmap(
            qta.icon("fa6s.file-lines", color="#1769e0").pixmap(48, 48)
        )
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_icon)
        self.empty_title = QLabel("Расшифровки нет"); self.empty_title.setObjectName("emptyTitle"); self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter); empty.addWidget(self.empty_title)
        self.empty_desc = QLabel("Выберите модель и запустите расшифровку"); self.empty_desc.setObjectName("muted"); self.empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter); self.empty_desc.setWordWrap(True); self.empty_desc.setFixedWidth(354); empty.addWidget(self.empty_desc)
        empty.addSpacing(18); self.model_label = QLabel("Модель"); self.model_label.setObjectName("fieldLabel"); empty.addWidget(self.model_label)
        self.model = QComboBox(); self.model.setAccessibleName("Модель расшифровки"); self._populate_installed_models(); self.model.currentIndexChanged.connect(lambda: self.model_selected.emit(self.model.currentData())); self.model.setFixedWidth(354); empty.addWidget(self.model)
        self.transcribe = QPushButton("Расшифровать"); self.transcribe.setObjectName("primary"); self.transcribe.setFixedWidth(354); self.transcribe.clicked.connect(self._request_transcription); empty.addWidget(self.transcribe)
        self.transcribe.setIconSize(QSize(14, 14))
        self._set_transcribe_button_icon("ready")
        self.folder_button = QPushButton("Открыть папку"); self.folder_button.setIcon(self._icon(QStyle.StandardPixmap.SP_DirOpenIcon)); self.folder_button.setObjectName("linkButton"); self.folder_button.setFixedWidth(354); self.folder_button.clicked.connect(self._open_recording_folder); empty.addWidget(self.folder_button)
        layout.addWidget(self.empty, 1)
        self.transcript = QTextEdit(); self.transcript.setReadOnly(True); self.transcript.hide(); layout.addWidget(self.transcript, 1)
        self.recording_bar = QFrame()
        self.recording_bar.setObjectName("recordingBar")
        self.recording_bar.setMinimumHeight(62)
        bottom = QHBoxLayout(self.recording_bar)
        bottom.setContentsMargins(14, 10, 14, 10)
        bottom.setSpacing(12)
        self.screen = QCheckBox("Экран и звук")
        self.screen.setChecked(settings_manager.load_all_settings().get(SettingsKey.VIDEO_RECORDING_ENABLED, config.VIDEO_RECORDING_ENABLED))
        self.screen.setToolTip("Записать экран, микрофон и звук встречи в MP4")
        bottom.addWidget(self.screen)
        bottom.addStretch()
        self.processing_actions = QFrame()
        self.processing_actions.setObjectName("processingActions")
        processing_layout = QHBoxLayout(self.processing_actions)
        processing_layout.setContentsMargins(12, 5, 6, 5)
        processing_layout.setSpacing(8)
        self.processing_status = QLabel("Обработка текста")
        self.processing_status.setObjectName("processingStatus")
        processing_layout.addWidget(self.processing_status)
        self.cancel_processing = QPushButton("Отмена")
        self.cancel_processing.setObjectName("cancelProcessing")
        self.cancel_processing.setAccessibleName("Отменить обработку")
        self.cancel_processing.clicked.connect(self.cancel_requested.emit)
        processing_layout.addWidget(self.cancel_processing)
        self.processing_actions.hide()
        bottom.addWidget(self.processing_actions)
        self.record_actions = QWidget()
        self.record_actions.setObjectName("recordActions")
        record_actions_layout = QHBoxLayout(self.record_actions)
        record_actions_layout.setContentsMargins(0, 0, 0, 0)
        record_actions_layout.setSpacing(8)
        self.record = QPushButton("Записать встречу")
        self.record.setIcon(qta.icon("fa6s.circle", color="#ffffff"))
        self.record.setObjectName("primary")
        self.record.setFixedWidth(174)
        self.record.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        self.record.setToolTip("Начать запись встречи")
        self.record.clicked.connect(self.record_requested.emit)
        self.stop_record = QPushButton("Остановить")
        self.stop_record.setIcon(qta.icon("fa6s.stop", color="#ffffff"))
        self.stop_record.setObjectName("stopButton")
        self.stop_record.setFixedWidth(174)
        self.stop_record.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        self.stop_record.setToolTip("Завершить запись и перейти к расшифровке")
        self.stop_record.clicked.connect(self.stop_requested.emit)
        self.stop_record.setEnabled(False)
        record_actions_layout.addWidget(self.record)
        record_actions_layout.addWidget(self.stop_record)
        bottom.addWidget(self.record_actions)
        bottom.addStretch()
        layout.addWidget(self.recording_bar)
        records_layout.addWidget(main, 1)
        self.content_stack.addWidget(records_page)
        self._page_widgets["records"] = records_page
        root.addWidget(self.content_stack, 1)

    def show_page(self, page_key):
        """Switch the right-hand content without opening another window."""
        page = self._page_widgets.get(page_key)
        if page is None:
            return
        self.content_stack.setCurrentWidget(page)
        for key, button in self._nav_buttons.items():
            button.setProperty("active", key == page_key)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()
        self._refresh_nav_icons()

    def set_embedded_page(self, page_key, widget, activate=True):
        """Register an embedded page and optionally make it active."""
        if self.content_stack.indexOf(widget) < 0:
            self.content_stack.addWidget(widget)
        self._page_widgets[page_key] = widget
        if activate:
            self.show_page(page_key)

    def _refresh_nav_icons(self):
        inactive = "#9aa8bc" if self.dark else "#46505e"
        for key, button in self._nav_buttons.items():
            icon_name = button.property("iconKind")
            if not icon_name:
                continue
            icon_kind = getattr(QStyle.StandardPixmap, icon_name)
            color = "#60a5fa" if self.dark and button.property("active") else (
                "#1769e0" if button.property("active") else inactive
            )
            button.setIcon(self._icon(icon_kind, color))

    def _refresh_action_icons(self):
        """Keep action meaning, weight, and optical size consistent."""
        accent = "#60a5fa" if self.dark else "#1769e0"
        danger = "#f87171" if self.dark else "#d84747"
        muted = "#617086" if self.dark else "#a0a8b4"
        for button in (
            self.open_media_button,
            self.codex_improve_button,
            self.trash_button,
        ):
            color = danger if button.property("iconTone") == "danger" else accent
            button.setIcon(
                qta.icon(
                    button.property("iconName"),
                    color=color,
                    color_disabled=muted,
                )
            )

    def _apply_theme(self):
        bg, panel, text, muted, border, hover, select, accent, danger = (
            ("#111722", "#17202d", "#f3f6fb", "#9aa8bc", "#2b3749", "#1d2939", "#203b5d", "#60a5fa", "#f87171")
            if self.dark
            else ("#fbfcfe", "#f4f7fb", "#18202b", "#6f7b8d", "#e2e8f0", "#edf3fa", "#e4efff", "#1769e0", "#d84747")
        )
        self.setStyleSheet(f"""
            QWidget#voiceNotesWorkspace,QStackedWidget#workspaceStack,QWidget#recordsPage,QWidget#main,QWidget#empty {{ background:{bg}; color:{text}; font-family:'Segoe UI'; font-size:14px; }}
            QLabel {{ background:transparent; color:{text}; }}
            QFrame#nav {{ background:{panel}; border-right:1px solid {border}; }} QFrame#list {{ background:{bg}; border-right:1px solid {border}; }}
            QLabel#sectionTitle {{ font-size:18px; font-weight:600; }} QLabel#noteName {{ font-size:28px; font-weight:600; }}
            QPushButton#navButton {{ background:transparent; border:0; border-radius:10px; padding:11px 12px; text-align:left; font-weight:400; }} QPushButton#navButton:hover {{ background:{hover}; }} QPushButton#navButton[active='true'] {{ background:{select}; color:{accent}; }}
            QPushButton#themeButton,QPushButton#openMediaButton,QPushButton#codexImproveButton,QPushButton#trashMeetingButton,QPushButton#iconButton,QPushButton#playButton,QPushButton#linkButton {{ border:0; background:transparent; color:{accent}; padding:7px; border-radius:10px; }}
            QPushButton#themeButton:hover,QPushButton#openMediaButton:hover,QPushButton#codexImproveButton:hover,QPushButton#trashMeetingButton:hover,QPushButton#iconButton:hover,QPushButton#playButton:hover,QPushButton#linkButton:hover {{ background:{hover}; }}
            QPushButton#themeButton:pressed,QPushButton#openMediaButton:pressed,QPushButton#codexImproveButton:pressed,QPushButton#trashMeetingButton:pressed,QPushButton#iconButton:pressed,QPushButton#playButton:pressed {{ background:{select}; }}
            QPushButton#openMediaButton:disabled,QPushButton#codexImproveButton:disabled,QPushButton#trashMeetingButton:disabled,QPushButton#playButton:disabled {{ background:transparent; color:{muted}; }}
            QPushButton#trashMeetingButton {{ color:{danger}; }}
            QListWidget#notes {{ border:0; outline:0; background:{bg}; }} QListWidget#notes::item {{ border:0; border-radius:11px; margin:4px 0; padding:16px 12px; }} QListWidget#notes::item:hover {{ background:{hover}; }} QListWidget#notes::item:selected {{ background:{select}; color:{text}; }}
            QFrame#player {{ border-bottom:1px solid {border}; }} QLabel#wave {{ color:{accent}; font-size:17px; }} QLabel#source,QLabel#muted {{ color:{muted}; }} QLabel#emptyTitle {{ font-size:25px; font-weight:600; }} QLabel#fieldLabel {{ font-weight:600; }}
            QFrame#recordingBar {{ background:{panel}; border:1px solid {border}; border-radius:14px; }}
            QWidget#recordActions {{ background:transparent; border:0; }}
            QPushButton#primary {{ background:{accent}; color:#fff; border:0; border-radius:10px; padding:11px 18px; font-weight:600; text-align:center; }} QPushButton#primary:hover {{ background:#095aca; }} QPushButton#primary:disabled {{ background:{border}; color:{muted}; }} QPushButton#stopButton {{ background:{danger}; color:#fff; border:0; border-radius:10px; padding:11px 18px; font-weight:600; text-align:center; }} QPushButton#stopButton:hover {{ background:#c83737; }} QPushButton#stopButton:pressed {{ background:#ad2d2d; }} QPushButton#stopButton:disabled {{ background:{border}; color:{muted}; }} QTextEdit {{ border:0; background:transparent; font-size:16px; }} QCheckBox {{ background:transparent; color:{muted}; }} QLabel:disabled,QCheckBox:disabled {{ color:{muted}; }}
            QFrame#processingActions {{ background:{panel}; border:1px solid {border}; border-radius:12px; }} QLabel#processingStatus {{ color:{muted}; }} QPushButton#cancelProcessing {{ border:0; background:transparent; color:{danger}; padding:5px 8px; font-weight:600; }} QPushButton#cancelProcessing:hover {{ background:{hover}; border-radius:7px; }}
        """)
        self.transcript.document().setDefaultStyleSheet(f"""
            body {{
                color: {text};
                font-family: 'Segoe UI';
                font-size: 16px;
                line-height: 145%;
            }}
            h1, h2, h3 {{
                color: {text};
                font-weight: 650;
                margin-top: 16px;
                margin-bottom: 10px;
            }}
            h1 {{ font-size: 24px; }}
            h2 {{ font-size: 21px; }}
            h3 {{ font-size: 18px; }}
            p {{ margin-top: 0; margin-bottom: 9px; }}
            ul, ol {{ margin-top: 4px; margin-bottom: 10px; }}
            li {{ margin-bottom: 5px; }}
            strong {{ font-weight: 650; }}
        """)
        self.transcript.document().setDocumentMargin(18)
        self._apply_transcript_typography()
        self.theme_button.setToolTip(
            "Включить светлую тему" if self.dark else "Включить тёмную тему"
        )
        self.theme_button.setAccessibleName(
            "Включить светлую тему" if self.dark else "Включить тёмную тему"
        )
        self.theme_button.setIcon(
            qta.icon(
                "fa6s.sun" if self.dark else "fa6s.moon",
                color="#9aa8bc" if self.dark else "#46505e",
            )
        )
        self.waveform.set_color(accent)
        self._refresh_action_icons()
        self._update_play_button(self._media_player.playbackState())
        self._set_empty_state_icon(
            "error" if self._transcription_state == "error" else (
                "busy"
                if self._transcription_state
                in {"processing", "transcribing", "cleaning"}
                else "ready"
            )
        )
        self._refresh_nav_icons()
        self._apply_transcription_controls_state()

    def set_theme(self, theme): self.dark = theme == "dark"; self._apply_theme()
    def toggle_theme(self): self.dark = not self.dark; self._apply_theme(); self.theme_changed.emit("dark" if self.dark else "light")
    @staticmethod
    def _format_time(seconds):
        seconds = max(0, int(seconds or 0))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _clean_transcript_title(value):
        """Remove Markdown decoration from a transcript-derived title."""
        title = (value or "").strip()
        title = re.sub(r"^\s{0,3}#{1,6}\s*", "", title)
        title = re.sub(r"^\s*[-*+]\s+", "", title)
        title = title.strip("*_` ")
        return title or "Встреча без названия"

    def _apply_transcript_typography(self):
        """Give every transcript comfortable line and paragraph spacing."""
        document = self.transcript.document()
        block = document.begin()
        while block.isValid():
            cursor = QTextCursor(block)
            block_format = cursor.blockFormat()
            block_format.setLineHeight(
                145,
                QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
            )
            block_format.setBottomMargin(5)
            cursor.setBlockFormat(block_format)
            block = block.next()

    def _show_transcript_text(self, text, transcript_format=""):
        if transcript_format == ".md":
            self.transcript.setMarkdown(text)
        else:
            self.transcript.setPlainText(text)
        self._apply_transcript_typography()
        self._highlight_search_matches(scroll_to_first=True)

    @staticmethod
    def _media_duration(path):
        if not path or not os.path.exists(path):
            return 0.0
        try:
            import av

            with av.open(path) as media:
                if media.duration is not None:
                    return float(media.duration) / 1_000_000
                streams = list(media.streams.audio) + list(media.streams.video)
                for stream in streams:
                    if stream.duration is not None and stream.time_base:
                        return float(stream.duration * stream.time_base)
        except Exception:
            return 0.0
        return 0.0

    def _toggle_playback(self):
        if not self._selected_media_path or not os.path.exists(
            self._selected_media_path
        ):
            return
        if self._media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._media_player.pause()
            return
        current_source = self._media_player.source().toLocalFile()
        if os.path.normcase(current_source) != os.path.normcase(
            self._selected_media_path
        ):
            self._media_player.setSource(
                QUrl.fromLocalFile(self._selected_media_path)
            )
        self._media_player.play()

    def _update_play_button(self, state):
        icon_name = "fa6s.pause" if state == QMediaPlayer.PlaybackState.PlayingState else "fa6s.play"
        self.play_button.setIcon(
            qta.icon(icon_name, color="#60a5fa" if self.dark else "#1769e0")
        )
        self.play_button.setAccessibleName(
            "Пауза"
            if state == QMediaPlayer.PlaybackState.PlayingState
            else "Воспроизвести"
        )

    def _update_recording_timer(self):
        if self.recording:
            self.elapsed_label.setText(self._format_time(time.monotonic() - self._record_started_at))
    def _request_transcription(self):
        if self._transcription_state in {"processing", "transcribing", "cleaning"}:
            return
        if self._selected_audio_path and os.path.exists(self._selected_audio_path):
            audio_path = self._selected_audio_path
            self.set_transcription_state("processing", audio_path)
            self.transcribe_requested.emit(audio_path)

    def _set_transcribe_button_icon(self, state):
        icons = {
            "ready": ("fa6s.file-lines", "#ffffff"),
            "busy": ("fa6s.hourglass-half", "#8e99a8"),
            "error": ("fa6s.rotate-right", "#ffffff"),
            "complete": ("fa6s.check", "#ffffff"),
        }
        icon_name, color = icons.get(state, icons["ready"])
        self.transcribe.setIcon(qta.icon(icon_name, color=color))

    def _set_empty_state_icon(self, state):
        icons = {
            "ready": (
                "fa6s.file-lines",
                "#60a5fa" if self.dark else "#1769e0",
            ),
            "busy": (
                "fa6s.hourglass-half",
                "#7f8a99" if self.dark else "#768292",
            ),
            "error": ("fa6s.triangle-exclamation", "#b66a22"),
        }
        icon_name, color = icons.get(state, icons["ready"])
        self.empty_icon.setPixmap(qta.icon(icon_name, color=color).pixmap(48, 48))

    @staticmethod
    def _same_path(first, second):
        if not first or not second:
            return False
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
            os.path.abspath(second)
        )

    def set_transcription_state(self, state, audio_path="", message=""):
        """Apply a stable, non-animated state to the transcription controls."""
        valid_states = {
            "idle",
            "processing",
            "transcribing",
            "cleaning",
            "complete",
            "error",
            "canceled",
        }
        self._transcription_state = state if state in valid_states else "idle"
        if audio_path:
            self._active_transcription_path = audio_path
        if self._transcription_state == "error":
            self._transcription_error = message or "Не удалось расшифровать файл"
        elif self._transcription_state not in {"processing", "transcribing", "cleaning"}:
            self._transcription_error = ""
        self._apply_transcription_controls_state()

    def _apply_transcription_controls_state(self):
        if not hasattr(self, "transcribe"):
            return
        busy = self._transcription_state in {"processing", "transcribing", "cleaning"}
        selected_is_active = self._same_path(
            self._selected_audio_path, self._active_transcription_path
        )
        has_source = bool(
            self._selected_audio_path
            and os.path.exists(self._selected_audio_path)
        )

        self.model.setEnabled(not busy and not self.recording)
        self.record.setEnabled(not busy and not self.recording)
        self.trash_button.setEnabled(not busy and not self.recording)
        codex_enabled = resolve_codex_cleanup_enabled()
        self.codex_improve_button.setEnabled(
            codex_enabled and not busy and not self.recording
        )
        self.codex_improve_button.setToolTip(
            "Создать улучшенную версию без повторной расшифровки"
            if codex_enabled
            else "Сначала включите Codex в настройках обработки текста"
        )
        self.stop_record.setEnabled(self.recording)
        self.record_actions.setVisible(not busy)
        self.processing_actions.setVisible(busy)
        if self._transcription_state == "cleaning":
            self.processing_status.setText("Обработка текста в Codex…")
        else:
            self.processing_status.setText("Расшифровка…")
        self.record.setToolTip(
            "Дождитесь завершения расшифровки"
            if busy
            else "Начать запись встречи"
        )
        if self.recording:
            self.transcribe.setEnabled(False)
            self.transcribe.setText("Идёт запись")
            self.transcribe.setToolTip(
                "Остановите запись перед расшифровкой другой встречи"
            )
            self._set_transcribe_button_icon("busy")
            self.empty_icon.setPixmap(
                qta.icon("fa6s.microphone", color="#d84747").pixmap(48, 48)
            )
        elif busy:
            self.transcribe.setEnabled(False)
            self.transcribe.setText(
                "Расшифровка…"
                if selected_is_active
                else "Идёт другая расшифровка"
            )
            self.transcribe.setToolTip("Дождитесь завершения текущей расшифровки")
            self._set_transcribe_button_icon("busy")
            self._set_empty_state_icon("busy")
            if selected_is_active and not self.transcript.isVisible():
                self.empty_title.setText(
                    "Обработка текста"
                    if self._transcription_state == "cleaning"
                    else "Расшифровка"
                )
                self.empty_desc.setText(
                    "Codex приводит расшифровку в порядок"
                    if self._transcription_state == "cleaning"
                    else "Обрабатываем запись"
                )
        elif self._transcription_state == "error" and selected_is_active:
            self.transcribe.setEnabled(has_source)
            self.transcribe.setText("Повторить")
            self.transcribe.setToolTip("Повторить расшифровку")
            self._set_transcribe_button_icon("error")
            self._set_empty_state_icon("error")
            if not self.transcript.isVisible():
                self.empty_title.setText("Не удалось расшифровать")
                self.empty_desc.setText(self._transcription_error)
        else:
            self.transcribe.setEnabled(has_source)
            self.transcribe.setText("Расшифровать")
            self.transcribe.setToolTip("Запустить расшифровку выбранной встречи")
            self._set_transcribe_button_icon("ready")
            self._set_empty_state_icon("ready")

        visual_state = (
            "busy"
            if busy or self.recording
            else "error"
            if self._transcription_state == "error" and selected_is_active
            else "ready"
        )
        self.transcribe.setProperty("state", visual_state)
        self.transcribe.style().unpolish(self.transcribe)
        self.transcribe.style().polish(self.transcribe)
        self.transcribe.update()

    def _populate_installed_models(self):
        names = {"tiny": "Whisper tiny — самый быстрый", "base": "Whisper base — быстрый", "small": "Whisper small — оптимальный", "medium": "Whisper medium — точный", "turbo": "Whisper turbo — быстрый и точный"}
        current = settings_manager.get(SettingsKey.WHISPER_MODEL, "turbo")
        installed = [name for name in names if name == current or is_model_cached(name)]
        if not installed: installed = [current]
        for name in installed: self.model.addItem(names.get(name, f"Whisper {name}"), name)
        index = self.model.findData(current)
        self.model.setCurrentIndex(max(0, index))

    def _open_recording_folder(self):
        selected_path = self._selected_media_path or self._selected_audio_path
        folder = os.path.dirname(selected_path) if selected_path else history_manager.recordings_folder
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _open_selected_media(self):
        if self._selected_media_path and os.path.exists(
            self._selected_media_path
        ):
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(self._selected_media_path)
            )

    def _request_codex_improvement(self):
        source_path = self._selected_audio_path or self._selected_media_path
        if (
            not self._selected_transcript_text.strip()
            or self._selected_enhanced_by_codex
            or NO_SPEECH_TRANSCRIPT in self._selected_transcript_text
        ):
            return
        self.codex_improve_requested.emit(
            source_path,
            self._selected_transcript_text,
            self._selected_history_id,
        )

    def _move_selected_to_trash(self):
        source_path = self._selected_media_path or self._selected_audio_path
        if not source_path:
            return
        meeting_name = self.note_name.text()
        answer = QMessageBox.question(
            self,
            "Переместить встречу в корзину?",
            (
                f"«{meeting_name}» будет перемещена в корзину вместе "
                "с аудио, видео и всеми вариантами расшифровки.\n\n"
                "При необходимости файлы можно восстановить из корзины."
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._media_player.stop()
        self._media_player.setSource(QUrl())
        try:
            history_manager.move_meeting_to_trash(
                source_path,
                self._selected_history_id,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Не удалось переместить встречу",
                str(exc),
            )
            return
        self._selected_audio_path = ""
        self._selected_media_path = ""
        self._selected_history_id = ""
        self.refresh_history()

    @staticmethod
    def _search_terms(text):
        """Return unique, case-insensitive terms from a meeting query."""
        terms = []
        for part in re.split(r"\s+", (text or "").casefold().strip()):
            if part and part not in terms:
                terms.append(part)
        return terms

    def _highlight_search_matches(self, scroll_to_first=False):
        """Outline every search match in the currently displayed transcript."""
        if not hasattr(self, "transcript"):
            return
        terms = self._search_terms(
            self.search.text() if hasattr(self, "search") else ""
        )
        document = self.transcript.document()
        plain_text = document.toPlainText()
        selections = []
        first_position = None

        if terms and plain_text:
            fill = QColor("#5a4318" if self.dark else "#fff0a8")
            outline = QColor("#ffc04d" if self.dark else "#a95e00")
            match_format = QTextCharFormat()
            match_format.setBackground(fill)
            outline_pen = QPen(outline)
            outline_pen.setWidthF(0.8)
            match_format.setTextOutline(outline_pen)

            for term in terms:
                for match in re.finditer(
                    re.escape(term),
                    plain_text,
                    flags=re.IGNORECASE,
                ):
                    cursor = QTextCursor(document)
                    cursor.setPosition(match.start())
                    cursor.setPosition(
                        match.end(),
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    selection = QTextEdit.ExtraSelection()
                    selection.cursor = cursor
                    selection.format = match_format
                    selections.append(selection)
                    if first_position is None or match.start() < first_position:
                        first_position = match.start()

        self.transcript.setExtraSelections(selections)
        if scroll_to_first and first_position is not None:
            cursor = QTextCursor(document)
            cursor.setPosition(first_position)
            self.transcript.setTextCursor(cursor)
            self.transcript.ensureCursorVisible()

    def _filter_notes(self, text):
        terms = self._search_terms(text)
        first_visible = None
        for index in range(self.notes.count()):
            item = self.notes.item(index)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            searchable_text = (
                f"{item.text()}\n{data.get('text') or ''}"
            ).casefold()
            matches = all(term in searchable_text for term in terms)
            item.setHidden(bool(terms) and not matches)
            if matches and first_visible is None:
                first_visible = item

        current = self.notes.currentItem()
        if terms and first_visible is not None and (
            current is None or current.isHidden()
        ):
            self.notes.setCurrentItem(first_visible)
        else:
            self._highlight_search_matches(scroll_to_first=bool(terms))

    @staticmethod
    def _meeting_size(*paths):
        """Return the total size of distinct existing media files."""
        total = 0
        seen = set()
        for path in paths:
            if not path:
                continue
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized in seen:
                continue
            seen.add(normalized)
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total

    @staticmethod
    def _meeting_timestamp(timestamp, *paths):
        modified = []
        for path in paths:
            if not path:
                continue
            try:
                modified.append(os.path.getmtime(path))
            except OSError:
                pass
        if modified:
            return time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(max(modified))
            )
        return str(timestamp or "")

    def _sort_notes(self):
        """Sort the combined database and folder-backed meeting list."""
        if not hasattr(self, "notes") or self.notes.count() < 2:
            return
        mode = self.sort.currentData() or self.SORT_NEWEST
        current = self.notes.currentItem()
        previous_blocked = self.notes.blockSignals(True)
        items = [self.notes.takeItem(0) for _ in range(self.notes.count())]

        def data_for(item):
            return item.data(Qt.ItemDataRole.UserRole) or {}

        if mode == self.SORT_OLDEST:
            items.sort(
                key=lambda item: (
                    not bool(data_for(item).get("timestamp")),
                    data_for(item).get("timestamp") or "",
                    item.text().casefold(),
                )
            )
        else:
            field = {
                self.SORT_SIZE: "size",
                self.SORT_DURATION: "duration",
            }.get(mode, "timestamp")
            items.sort(
                key=lambda item: (
                    data_for(item).get(field) or 0,
                    data_for(item).get("timestamp") or "",
                    item.text().casefold(),
                ),
                reverse=True,
            )
        for item in items:
            self.notes.addItem(item)
        self.notes.blockSignals(previous_blocked)
        if current is not None:
            self.notes.setCurrentItem(current)

    def refresh_history(self):
        selected_path = self._selected_media_path or self._selected_audio_path
        selected_history_id = self._selected_history_id
        self.notes.blockSignals(True); self.notes.clear()
        seen = set()
        for entry in history_manager.get_history():
            audio_path = history_manager.get_recording_path(entry.audio_file) if entry.audio_file else ""
            video_path = ""
            if audio_path:
                extension = os.path.splitext(audio_path)[1].lower()
                if extension in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}:
                    video_path = audio_path
                else:
                    sidecar = os.path.splitext(audio_path)[0] + ".mp4"
                    if os.path.exists(sidecar):
                        video_path = sidecar
                seen.add(os.path.normcase(audio_path))
                if video_path:
                    seen.add(os.path.normcase(video_path))
            media_path = video_path or audio_path
            display_text = entry.text or ""
            enhanced_by_codex = entry.cleanup_provider == "codex"
            transcript_format = ".md" if enhanced_by_codex else ".txt"
            if audio_path:
                codex_path = os.path.splitext(audio_path)[0] + ".codex.md"
                codex_text = history_manager.read_transcript(codex_path)
                if codex_text:
                    display_text = codex_text
                    enhanced_by_codex = True
                    transcript_format = ".md"
                elif not display_text.strip():
                    # The recording can have an older empty database row while
                    # a later transcription attempt created a valid .txt
                    # sidecar, including the explicit no-speech marker.
                    transcript_path = os.path.splitext(audio_path)[0] + ".txt"
                    sidecar_text = history_manager.read_transcript(
                        transcript_path
                    )
                    if history_manager.has_transcript_content(sidecar_text):
                        display_text = sidecar_text
                        transcript_format = ".txt"
            no_speech = NO_SPEECH_TRANSCRIPT in display_text
            if no_speech:
                display_text = NO_SPEECH_TRANSCRIPT
            has_transcript = bool(display_text.strip())
            fallback_title = (
                os.path.splitext(os.path.basename(audio_path))[0]
                if audio_path
                else "Встреча без названия"
            )
            first_line = display_text.strip().splitlines()[0] if has_transcript else ""
            title_from_metadata = bool(re.match(
                r"^(?:исходник|source|обработка|processing)\s*:",
                first_line.strip(),
                flags=re.IGNORECASE,
            ))
            title = (
                self._clean_transcript_title(first_line)
                if has_transcript and not no_speech and not title_from_metadata
                else fallback_title
            )[:44]
            # Do not probe every external media file during startup. Some
            # damaged meeting containers can crash native decoders; the Qt
            # player supplies the duration safely when a user selects one.
            seconds = float(entry.audio_duration or 0)
            duration = self._format_time(seconds)
            size_bytes = self._meeting_size(audio_path, video_path)
            if not size_bytes:
                size_bytes = int(getattr(entry, "file_size", 0) or 0)
            timestamp = self._meeting_timestamp(
                getattr(entry, "timestamp", ""),
                media_path,
                audio_path,
            )
            meeting_date = format_timestamp(timestamp)
            size = format_file_size(size_bytes) if size_bytes else "—"
            transcript_status = (
                "Речь не обнаружена"
                if no_speech
                else "Улучшено в Codex"
                if has_transcript and enhanced_by_codex
                else "Расшифровано" if has_transcript
                else "Нет расшифровки"
            )
            item = QListWidgetItem(
                f"{title}\n{meeting_date}  ·  "
                f"{duration}  ·  {size}  ·  {transcript_status}"
            )
            item.setData(Qt.ItemDataRole.UserRole, {
                "id": entry.id,
                "audio": audio_path,
                "media": media_path,
                "video": video_path,
                "text": display_text,
                "transcript_format": transcript_format,
                "enhanced_by_codex": enhanced_by_codex,
                "model": entry.model,
                "duration": seconds,
                "size": size_bytes,
                "timestamp": timestamp,
            })
            self.notes.addItem(item)
        for recording in history_manager.get_media_files():
            transcript_text = history_manager.read_transcript(
                recording.transcript_path
            )
            if not history_manager.has_transcript_content(transcript_text):
                transcript_text = ""
            no_speech = NO_SPEECH_TRANSCRIPT in transcript_text
            if no_speech:
                transcript_text = NO_SPEECH_TRANSCRIPT
            paths = {
                os.path.normcase(path)
                for path in (
                    recording.file_path,
                    recording.transcription_path,
                    recording.audio_path,
                    recording.video_path,
                )
                if path
            }
            if paths & seen:
                continue
            seen.update(paths)
            media_label = "Видео" if recording.media_type == "video" else "Аудио"
            transcript_status = (
                "Речь не обнаружена"
                if no_speech
                else "Улучшено в Codex"
                if transcript_text
                and ".codex." in os.path.basename(
                    recording.transcript_path or ""
                ).casefold()
                else "Расшифровано" if transcript_text
                else "Нет расшифровки"
            )
            item = QListWidgetItem(
                f"{os.path.splitext(recording.filename)[0]}\n"
                f"{recording.formatted_timestamp}  ·  {recording.formatted_size}  ·  "
                f"{transcript_status}"
            )
            item.setData(Qt.ItemDataRole.UserRole, {
                "id": recording.file_path,
                "audio": recording.transcription_path,
                "media": recording.file_path,
                "video": recording.video_path or "",
                "text": transcript_text,
                "transcript_path": recording.transcript_path or "",
                "transcript_format": os.path.splitext(
                    recording.transcript_path or ""
                )[1].lower(),
                "enhanced_by_codex": (
                    ".codex." in os.path.basename(
                        recording.transcript_path or ""
                    ).casefold()
                ),
                "model": "",
                "duration": 0.0,
                "size": recording.size_bytes,
                "timestamp": recording.timestamp,
            })
            self.notes.addItem(item)
        self._sort_notes()
        self.notes.blockSignals(False)
        self.search.setVisible(self.notes.count() > 0)
        restored_item = None
        if selected_path or selected_history_id:
            for index in range(self.notes.count()):
                item = self.notes.item(index)
                data = item.data(Qt.ItemDataRole.UserRole) or {}
                if (
                    selected_history_id
                    and data.get("id") == selected_history_id
                ):
                    restored_item = item
                    break
                candidates = (
                    data.get("media"),
                    data.get("audio"),
                    data.get("video"),
                    data.get("id"),
                )
                if any(
                    self._same_path(selected_path, candidate)
                    for candidate in candidates
                    if candidate
                ):
                    restored_item = item
                    break
        if restored_item is not None:
            self.notes.setCurrentItem(restored_item)
            self._select_note(restored_item)
        elif self.notes.count():
            self._show_library_selection()
        else:
            self._show_no_selection()
        self._filter_notes(self.search.text())

    def _show_library_selection(self):
        self._selected_audio_path = ""
        self._selected_media_path = ""
        self._selected_history_id = ""
        self._selected_transcript_text = ""
        self._selected_enhanced_by_codex = False
        self.note_name.setText("Выберите встречу")
        self.open_media_button.hide()
        self.codex_improve_button.hide()
        self.trash_button.hide()
        self.source.hide()
        self.player.hide()
        self.play_button.setEnabled(False)
        self.transcript.hide()
        self.empty.show()
        self.empty_title.setText("Выберите встречу слева")
        self.empty_desc.setText("")
        self._set_transcription_controls_visible(False)
        self.transcribe.setEnabled(False)

    def _show_no_selection(self):
        self._selected_audio_path = ""
        self._selected_media_path = ""
        self._selected_history_id = ""
        self._selected_transcript_text = ""
        self._selected_enhanced_by_codex = False
        self.note_name.setText("Встреч пока нет")
        self.open_media_button.hide()
        self.codex_improve_button.hide()
        self.trash_button.hide()
        self.source.clear()
        self.source.hide()
        self.elapsed_label.setText("00:00")
        self.duration_label.setText("00:00")
        self.player.hide()
        self.play_button.setEnabled(False)
        self.transcript.hide()
        self.empty.show()
        self.empty_title.setText("Запишите первую встречу")
        self.empty_desc.setText("Нажмите «Записать встречу»")
        self._set_transcription_controls_visible(False)
        self.transcribe.setEnabled(False)

    def _set_transcription_controls_visible(self, visible):
        for widget in (
            self.model_label,
            self.model,
            self.transcribe,
            self.folder_button,
        ):
            widget.setVisible(visible)

    def _select_note(self, current, previous=None):
        if not current: self._show_no_selection(); return
        data = current.data(Qt.ItemDataRole.UserRole) or {}
        self._selected_history_id = data.get("id") or ""
        self._selected_transcript_text = data.get("text") or ""
        self._selected_enhanced_by_codex = bool(
            data.get("enhanced_by_codex")
        )
        self._selected_audio_path = data.get("audio") or ""
        self._selected_media_path = (
            data.get("media") or self._selected_audio_path
        )
        self.note_name.setText(
            self._clean_transcript_title(current.text().splitlines()[0])
        )
        self.open_media_button.setVisible(
            bool(
                self._selected_media_path
                and os.path.exists(self._selected_media_path)
            )
        )
        self.trash_button.setVisible(
            bool(
                (self._selected_media_path or self._selected_audio_path)
                and os.path.exists(
                    self._selected_media_path or self._selected_audio_path
                )
            )
        )
        self.codex_improve_button.setVisible(
            bool(
                self._selected_transcript_text.strip()
                and not self._selected_enhanced_by_codex
                and NO_SPEECH_TRANSCRIPT
                not in self._selected_transcript_text
            )
        )
        self.player.show()
        self.source.setText(
            os.path.basename(self._selected_media_path) or "Файл не найден"
        )
        self.source.show()
        if (
            self._media_player.playbackState()
            != QMediaPlayer.PlaybackState.StoppedState
        ):
            self._media_player.stop()
        # Defer native decoder initialization until Play is pressed. Meeting
        # folders often contain partially recovered WebM files, and probing
        # all of them while populating the list is both slow and fragile.
        self.elapsed_label.setText("00:00")
        self.duration_label.setText(self._format_time(data.get("duration", 0)))
        self.play_button.setEnabled(
            bool(
                self._selected_media_path
                and os.path.exists(self._selected_media_path)
            )
        )
        text = data.get("text", "")
        if text:
            self.empty.hide()
            self.transcript.show()
            self._show_transcript_text(
                text,
                data.get("transcript_format") or "",
            )
        else:
            self.transcript.hide()
            self.empty.show()
            self.empty_title.setText("Расшифровки нет")
            self.empty_desc.setText("Выберите модель и запустите")
            self._set_transcription_controls_visible(True)
            self._apply_transcription_controls_state()
        self._apply_transcription_controls_state()

    def set_transcript(self, text):
        self.set_transcription_state(
            "complete", self._active_transcription_path or self._selected_audio_path
        )
        self.empty.hide()
        self.transcript.show()
        self._show_transcript_text(text)
    def set_recording(self, value):
        was_recording = self.recording
        self.recording = bool(value)
        self.screen.setEnabled(not self.recording)
        if value:
            self._media_player.stop()
            self.player.show()
            self.transcript.hide()
            self.empty.show()
            self._set_transcription_controls_visible(False)
            self.empty_icon.setPixmap(
                qta.icon("fa6s.microphone", color="#d84747").pixmap(48, 48)
            )
            self.empty_title.setText("Идёт запись")
            self.empty_desc.setText(
                "Нажмите «Остановить запись», когда закончите"
            )
            self._record_started_at = time.monotonic()
            self.elapsed_label.setText("00:00")
            self.duration_label.setText("идёт запись")
            self.note_name.setText("Идёт запись встречи")
            self.source.setText(
                "Экран, микрофон и звук компьютера"
                if self.screen.isChecked()
                else "Только микрофон"
            )
            self.source.show()
            self._record_timer.start()
        else:
            self._record_timer.stop()
        self._apply_transcription_controls_state()
        if was_recording and not self.recording:
            self.note_name.setText("Обработка записи")
            self.empty_title.setText("Сохраняем встречу")
            self.empty_desc.setText("Подготавливаем файл к расшифровке")
            self._set_empty_state_icon("busy")
