# RenderBrain v1.0.0 Release Checklist

## 1. Configuración de Entorno (.env)
- [x] El archivo `.env` se encuentra ignorado en `.gitignore` y `.dockerignore`.
- [x] `.env.example` contiene placeholders seguros (`change_me...`) y ningún secreto real.
- [x] Las contraseñas y API keys están aseguradas en memoria mediante `SecretStr` de Pydantic.

## 2. Bases de Datos y Estado (Backup/Restore)
- [x] **PostgreSQL**: Se configuró con volumen persistente (`renderbrain_pgdata`).
- [x] **Backup Postgres**: Definido mediante `scripts/backup_db.bat` (`pg_dump -Fc`).
- [x] **Restore Postgres**: Definido mediante `scripts/restore_db.bat` (`pg_restore --clean`).
- [x] **Validación DB Cero -> Head**: `uv run alembic upgrade head` es exitoso sobre instalaciones vacías.
- [x] **Redis (Event Bus Transitorio)**: Redis oficia como backend de transporte (pub/sub y colas temporales del Scheduler) y no como Storage Principal. Su estrategia de persistencia actual es `appendonly yes` (AOF).

**Recuperación ante fallos críticos:**
1. **PostgreSQL es durable**: La base de datos es la fuente de verdad.
2. **ProcessedSignal evita reprocesamiento**: Protege el trabajo ya completado si se inyectan mensajes redundantes.
3. **Pending messages sobreviven según AOF**: Si Redis reinicia, AOF recupera el estado más reciente de la cola.
4. **Pérdida total**: La pérdida total de Redis pierde definitivamente eventos en vuelo/memoria que no fueron procesados o volcados a AOF.
5. **Scheduler Checkpoint**: Las futuras ejecuciones del Scheduler podrían volver a observar el origen (dependiendo de la ventana temporal y del tipo de sensor), pero **NO se garantiza la recuperación absoluta** de todos los eventos perdidos transitoriamente. Redis _loss_ no es completamente inocuo en sistemas realtime.

## 3. Infraestructura y Seguridad (Docker Compose)
- [x] Los servicios core (`postgres`, `redis`) no exponen sus puertos al público. Están restringidos a `127.0.0.1` en entornos locales, e ideales para redes internas cerradas en producción real.
- [x] **TLS / HTTPS**: El acceso al Dashboard y endpoints protegidos por Basic Auth requiere despliegue tras un reverse proxy (NGINX/Traefik) con soporte HTTPS.

## 4. Funcionalidad / Smoke Tests
El script `scripts/smoke_v1.py` garantiza que:
- [x] `/health` reporta 200 (Proceso vivo).
- [x] `/ready` reporta 200 (PostgreSQL alcanzable).
- [x] Accesos API requieren autenticación válida (401 si falla, 200 en éxito).
- [x] Las migraciones de Alembic están exactamente en HEAD.
- [x] Workers y Schedulers levantan sin colapsos de configuración (Fail-Fast).

## 5. Validaciones Finales
- [x] Tests e2e y unitarios verdes (`uv run pytest` con 100% de éxito).
- [x] `docker compose config` válido y sin fugas de secrets.
- [x] Ningún consumo de créditos no autorizados de OpenAI o Apify (Zero reales).

> **STATUS: COMPLETE**. 
> RenderBrain v1 está liberado y listo para ambientes productivos simples.
