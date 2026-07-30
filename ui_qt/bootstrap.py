"""Qt application bootstrap and startup flow."""

from __future__ import annotations

import faulthandler
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import config
from ui_qt.startup_profiler import StartupProfiler

_CRASH_LOG_FILE = None
_QT_MESSAGE_HANDLER_INSTALLED = False
_INSTANCE_MUTEX = None
_INSTANCE_MUTEX_NAME = "Local\\MeetingRecorder.OpenWhisper.SingleInstance"
_SHUTDOWN_EVENT_HANDLE = None
_SHUTDOWN_TIMER = None
_SHUTDOWN_EVENT_NAME = "Local\\MeetingRecorder.OpenWhisper.Shutdown"
_SHUTDOWN_FOR_UNINSTALL_ARG = "--shutdown-for-uninstall"


def request_running_instance_shutdown(timeout_seconds: float = 8.0) -> bool:
    """Ask the installed Windows instance to exit and wait for its cleanup."""
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    event_modify_state = 0x0002
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenEventW(
        event_modify_state, False, _SHUTDOWN_EVENT_NAME
    )
    if not handle:
        return False

    try:
        if not kernel32.SetEvent(handle):
            return False
    finally:
        kernel32.CloseHandle(handle)

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        probe = kernel32.OpenEventW(
            synchronize, False, _SHUTDOWN_EVENT_NAME
        )
        if not probe:
            return True
        kernel32.CloseHandle(probe)
        time.sleep(0.1)
    return False


def install_shutdown_listener(qt_app) -> None:
    """Quit through Qt when the uninstaller signals the named event."""
    global _SHUTDOWN_EVENT_HANDLE, _SHUTDOWN_TIMER
    if sys.platform != "win32":
        return

    import ctypes
    from ctypes import wintypes
    from PyQt6.QtCore import QTimer

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateEventW(
        None, True, False, _SHUTDOWN_EVENT_NAME
    )
    if not handle:
        logging.warning("Could not create the uninstall shutdown event")
        return

    _SHUTDOWN_EVENT_HANDLE = handle
    timer = QTimer(qt_app.app)
    timer.setInterval(100)

    def check_shutdown_event() -> None:
        wait_object_0 = 0x00000000
        if (
            kernel32.WaitForSingleObject(_SHUTDOWN_EVENT_HANDLE, 0)
            == wait_object_0
        ):
            timer.stop()
            logging.info("Graceful shutdown requested by the uninstaller")
            qt_app.quit()

    timer.timeout.connect(check_shutdown_event)
    timer.start()
    _SHUTDOWN_TIMER = timer


def release_shutdown_listener() -> None:
    """Stop polling while keeping the event alive until process termination.

    The uninstaller waits for the named event to disappear. Windows closes the
    remaining handle during process teardown, after imported DLLs are unlocked;
    closing it here would let file removal race with Python interpreter exit.
    """
    global _SHUTDOWN_TIMER
    if _SHUTDOWN_TIMER is not None:
        _SHUTDOWN_TIMER.stop()
        _SHUTDOWN_TIMER = None


def _restore_existing_windows_instance() -> None:
    """Restore the largest existing main window when a second copy is run."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    candidates = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value != "Запись встреч":
            return True
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            area = max(0, rect.right - rect.left) * max(
                0, rect.bottom - rect.top
            )
            candidates.append((area, hwnd))
        return True

    user32.EnumWindows(visit, 0)
    if not candidates:
        return
    _area, hwnd = max(candidates)
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)


def acquire_single_instance() -> bool:
    """Prevent duplicate recorder processes and restore the existing window."""
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        _restore_existing_windows_instance()
        return False
    _INSTANCE_MUTEX = handle
    return True


def release_single_instance() -> None:
    """Release the Windows instance mutex during a normal shutdown."""
    global _INSTANCE_MUTEX
    if sys.platform != "win32" or not _INSTANCE_MUTEX:
        return
    import ctypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_INSTANCE_MUTEX)
    _INSTANCE_MUTEX = None


def setup_logging() -> None:
    """Setup application logging."""
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    file_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=level,
        format=config.LOG_FORMAT,
        handlers=[file_handler, logging.StreamHandler()],
        force=True,
    )
    _enable_crash_logging()
    _install_qt_message_handler()


def _enable_crash_logging() -> None:
    """Enable faulthandler crash logging for hard crashes."""
    global _CRASH_LOG_FILE

    try:
        crash_log_path = Path(config.LOG_FILE).with_suffix(".crash.log")
        _CRASH_LOG_FILE = open(crash_log_path, "a", buffering=1)
        faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)

        for sig in (signal.SIGSEGV, signal.SIGABRT, signal.SIGFPE, signal.SIGILL):
            try:
                faulthandler.register(sig, file=_CRASH_LOG_FILE, all_threads=True)
            except (AttributeError, RuntimeError, ValueError):
                pass

        logging.info(f"Faulthandler enabled for crash diagnostics: {crash_log_path}")
    except Exception as exc:
        logging.warning(f"Failed to enable faulthandler: {exc}")


def _install_qt_message_handler() -> None:
    """Route Qt warnings/errors to the Python logger."""
    global _QT_MESSAGE_HANDLER_INSTALLED

    if _QT_MESSAGE_HANDLER_INSTALLED:
        return

    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception as exc:
        logging.warning(f"Failed to install Qt message handler: {exc}")
        return

    def _qt_message_handler(msg_type, context, message) -> None:
        logger = logging.getLogger("qt")
        context_info = ""
        try:
            if context and (context.file or context.function or context.line):
                context_info = f" ({context.file}:{context.line} {context.function})"
        except Exception:
            context_info = ""

        text = f"{message}{context_info}"

        if msg_type == QtMsgType.QtDebugMsg:
            logger.debug(text)
        elif msg_type == QtMsgType.QtInfoMsg:
            logger.info(text)
        elif msg_type == QtMsgType.QtWarningMsg:
            logger.warning(text)
        elif msg_type == QtMsgType.QtCriticalMsg:
            logger.error(text)
        elif msg_type == QtMsgType.QtFatalMsg:
            logger.critical(text)
        else:
            logger.info(text)

    qInstallMessageHandler(_qt_message_handler)
    _QT_MESSAGE_HANDLER_INSTALLED = True
    logging.info("Qt message handler installed")


def get_early_runtime_components():
    """Load only the runtime classes needed for the first visual."""
    from ui_qt.app import QtApplication
    from ui_qt.loading_screen import LoadingScreen

    return QtApplication, LoadingScreen


def get_late_runtime_components():
    """Load heavier runtime classes after the loading screen is visible."""
    from services.application_controller import ApplicationController
    from ui_qt.ui_controller import UIController

    return UIController, ApplicationController


def process_qt_events() -> None:
    """Flush pending Qt events during startup."""
    from PyQt6.QtCore import QCoreApplication

    QCoreApplication.processEvents()


def load_local_whisper_backend():
    """Load the local Whisper backend (safe to call off the UI thread)."""
    from transcriber import LocalWhisperBackend

    return LocalWhisperBackend()


def run_with_ui_pulse(fn):
    """Run ``fn`` on a worker thread while keeping the splash animation alive.

    Startup previously blocked the UI thread on model load, so QTimer-driven
    painting never ran. This spins a nested event loop on the main thread until
    the worker finishes, which lets the loading-screen glow timer fire.

    Args:
        fn: Zero-arg callable. Must not touch Qt widgets/objects.

    Returns:
        The return value of ``fn``.

    Raises:
        Exception: Re-raises whatever ``fn`` raised on the worker thread.
    """
    import threading

    from PyQt6.QtCore import QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return fn()

    box = {"done": False, "result": None, "error": None}

    def worker() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - re-raised on main thread
            box["error"] = exc
        finally:
            box["done"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while not box["done"]:
        loop = QEventLoop()
        QTimer.singleShot(33, loop.quit)
        loop.exec()

    thread.join(timeout=1.0)

    if box["error"] is not None:
        raise box["error"]
    return box["result"]


def main() -> int:
    """Main application entry point."""
    if _SHUTDOWN_FOR_UNINSTALL_ARG in sys.argv[1:]:
        request_running_instance_shutdown()
        return 0
    if not acquire_single_instance():
        return 0
    profiler = StartupProfiler()
    profiler.mark("main_entered")
    summary_logged = False

    setup_logging()
    profiler.mark("logging_ready")
    logging.info("=" * 60)
    logging.info("Starting OpenWhisper")
    logging.info("=" * 60)

    # Model loads are cache-first (local_files_only) regardless of settings;
    # an external HF_HUB_OFFLINE=1 additionally hard-disables downloads.
    from services.settings import is_hf_hub_offline_env_set

    if is_hf_hub_offline_env_set():
        logging.info(
            "HF_HUB_OFFLINE set in environment — Hugging Face downloads disabled"
        )

    profiler.mark("early_imports_started")
    QtApplication, LoadingScreen = get_early_runtime_components()
    profiler.mark("early_imports_finished")

    qt_app = QtApplication()
    install_shutdown_listener(qt_app)
    profiler.mark("qt_app_created")
    loading_screen = None
    ui_controller = None
    app_controller = None

    try:
        loading_screen = LoadingScreen()
        profiler.mark("loading_screen_constructed")
        loading_screen.show()
        profiler.mark("loading_screen_shown")

        loading_screen.update_status("Подготовка компонентов…")
        loading_screen.update_progress("Запуск приложения…")
        loading_screen.repaint()
        process_qt_events()
        profiler.mark("first_visual_flushed")

        loading_screen.update_status("Загрузка приложения…")
        loading_screen.update_progress("Подключение компонентов…")
        process_qt_events()

        profiler.mark("late_imports_started")
        UIController, ApplicationController = run_with_ui_pulse(
            get_late_runtime_components
        )
        profiler.mark("late_imports_finished")

        loading_screen.update_status("Создание интерфейса…")
        loading_screen.update_progress("Настройка окон…")
        process_qt_events()

        ui_controller = UIController()
        profiler.mark("ui_controller_created")

        loading_screen.update_status("Подготовка аудио…")
        loading_screen.update_progress("Загрузка модели распознавания…")
        process_qt_events()

        local_backend = run_with_ui_pulse(load_local_whisper_backend)
        app_controller = ApplicationController(
            ui_controller, local_backend=local_backend
        )
        profiler.mark("application_controller_created")

        local_backend = app_controller.transcription_backends.get("local_whisper")
        if local_backend and hasattr(local_backend, "device_info"):
            device_info = local_backend.device_info
            loading_screen.update_progress(f"Модель готова: {device_info}")
            process_qt_events()
            logging.info(f"Whisper device: {device_info}")

        loading_screen.destroy()
        loading_screen = None

        ui_controller.show_main_window()
        profiler.mark("main_window_shown")

        if local_backend and hasattr(local_backend, "device_info"):
            ui_controller.set_device_info(local_backend.device_info)

        # Now that the main UI is available, a missing local model may request
        # download consent (never during startup, never for API-only users).
        app_controller.notify_main_ui_ready()
        ui_controller.schedule_startup_update_check()

        profiler.log_summary()
        summary_logged = True
        logging.info("Application initialization complete")
        logging.info("Starting event loop")
        return qt_app.run(ui_controller.main_window)
    except Exception:
        if not summary_logged:
            profiler.log_summary()
            summary_logged = True
        logging.exception("Application startup failed")
        raise
    finally:
        try:
            if loading_screen is not None:
                loading_screen.destroy()
        except Exception:
            logging.exception("Failed to cleanup loading screen")

        try:
            if app_controller is not None:
                app_controller.cleanup()
            elif ui_controller is not None:
                ui_controller.cleanup()
        except Exception:
            logging.exception("Failed to cleanup controllers")

        logging.info("=" * 60)
        logging.info("Application shutdown complete")
        logging.info("=" * 60)
        release_shutdown_listener()
        release_single_instance()
