@echo off
setlocal

if "%~1"=="" (
    echo Usage: restore_db.bat ^<target_database^> [backup_file.dump]
    echo Example: restore_db.bat renderbrain_restore_test
    exit /b 1
)

set TARGET_DB=%~1
set DUMP_FILE=%~2
if "%DUMP_FILE%"=="" set DUMP_FILE=renderbrain_backup.dump

if not exist "%DUMP_FILE%" (
    echo Error: File %DUMP_FILE% not found.
    exit /b 1
)

echo WARNING: You are about to restore %DUMP_FILE% into the target database '%TARGET_DB%'.
echo The --clean flag will DESTROY objects in the target database before restoring!
echo Press Ctrl+C to abort, or press any key to continue.
pause

echo Restoring %DUMP_FILE% to %TARGET_DB%...
docker exec -i renderbrain-postgres pg_restore -U renderbrain -d %TARGET_DB% --clean --if-exists --no-owner -1 < "%DUMP_FILE%"

if %ERRORLEVEL% equ 0 (
    echo Restore completed successfully.
) else (
    echo Restore failed!
    exit /b 1
)
