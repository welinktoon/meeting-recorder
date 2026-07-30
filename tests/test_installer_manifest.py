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
