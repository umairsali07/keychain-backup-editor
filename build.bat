@echo off
REM Build the standalone KeychainBackupEditor.exe.
REM Requires: Python 3.11+, pyinstaller (pip install -r requirements.txt),
REM and a built irestore.exe in this folder (the patched build).

if not exist irestore.exe (
    echo ERROR: irestore.exe not found in this folder.
    echo Put the patched irestore.exe here first - see docs\irestore-patches.md
    exit /b 1
)

python -m PyInstaller --noconfirm --onefile --windowed ^
    --name KeychainBackupEditor ^
    --add-binary "irestore.exe;." ^
    --distpath release ^
    --workpath build-tmp ^
    keychain_backup_editor.py

echo.
echo Done. Output: release\KeychainBackupEditor.exe
