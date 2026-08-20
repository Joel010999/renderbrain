@echo off
setlocal

set DUMP_FILE=renderbrain_backup.dump

echo Starting backup of renderbrain_db to %DUMP_FILE%...
docker exec renderbrain-postgres pg_dump -U renderbrain -d renderbrain_db -Fc > %DUMP_FILE%

if %ERRORLEVEL% equ 0 (
    echo Backup completed successfully: %DUMP_FILE%
) else (
    echo Backup failed!
)
