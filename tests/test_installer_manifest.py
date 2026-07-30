"""Windows installer contract tests."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_installer_offers_standard_shortcut_choices():
    manifest = (
        PROJECT_ROOT / "packaging" / "installer.iss"
    ).read_text(encoding="utf-8")

    assert 'Name: "desktopicon"' in manifest
    assert 'Description: "Создать ярлык на рабочем столе"' in manifest
    assert 'Name: "startmenuicon"' in manifest
    assert 'Description: "Добавить в меню «Пуск»"' in manifest
    assert 'Tasks: desktopicon' in manifest
    assert manifest.count("Tasks: startmenuicon") == 2
    assert 'Filename: "{uninstallexe}"' in manifest
    assert "{uninstalexe}" not in manifest
    desktop_task = next(
        line for line in manifest.splitlines()
        if 'Name: "desktopicon"' in line
    )
    assert "unchecked" not in desktop_task


def test_uninstaller_stops_the_running_tray_process_before_removing_files():
    manifest = (
        PROJECT_ROOT / "packaging" / "installer.iss"
    ).read_text(encoding="utf-8")

    uninstall_run = manifest.split("[UninstallRun]", 1)[1].split("[Run]", 1)[0]
    assert 'Filename: "{app}\\{#MyAppExeName}"' in uninstall_run
    assert 'Parameters: "--shutdown-for-uninstall"' in uninstall_run
    assert "Flags: runhidden waituntilterminated skipifdoesntexist" in uninstall_run
    assert 'RunOnceId: "GracefulStopMeetingRecorder"' in uninstall_run
    assert 'Filename: "{sys}\\taskkill.exe"' in uninstall_run
    assert 'Parameters: "/F /T /IM ""{#MyAppExeName}"""' in uninstall_run
    assert "Flags: runhidden waituntilterminated" in uninstall_run
    assert 'RunOnceId: "ForceStopMeetingRecorder"' in uninstall_run


def test_windows_release_bundles_the_gpu_runtime():
    workflow = (
        PROJECT_ROOT / ".github" / "workflows" / "windows-release.yml"
    ).read_text(encoding="utf-8")
    build_script = (
        PROJECT_ROOT / "scripts" / "build_windows.ps1"
    ).read_text(encoding="utf-8")
    spec = (
        PROJECT_ROOT / "packaging" / "meeting-recorder.spec"
    ).read_text(encoding="utf-8")
    entrypoint = (PROJECT_ROOT / "app_qt.py").read_text(encoding="utf-8")

    assert "-r requirements-gpu.txt" in workflow
    assert "  pull_request:" in workflow
    assert "-r requirements-gpu.txt" in build_script
    assert "Required CUDA runtime DLL was not bundled" in build_script
    for dll_name in (
        "cublas64_12.dll",
        "cudart64_12.dll",
        "cudnn64_9.dll",
    ):
        assert f'"{dll_name}"' in build_script
    for package_name in (
        "nvidia.cublas",
        "nvidia.cuda_nvrtc",
        "nvidia.cuda_runtime",
        "nvidia.cudnn",
    ):
        assert f'"{package_name}"' in spec
    assert 'getattr(sys, "_MEIPASS", None)' in entrypoint
