# RenderBrain - Content Intelligence Engine

## Ejecución de Servicios

El sistema requiere tres procesos para operar completamente:

1. **API y Dashboard:** (Maneja endpoints operacionales y visualización)
   ```bash
   uv run uvicorn runtime.api.main:app --reload
   ```
   > Dashboard disponible en `http://localhost:8000/dashboard`
   > Requiere autenticación HTTP Basic (ver `RENDERBRAIN_ADMIN_USERNAME` y `RENDERBRAIN_ADMIN_PASSWORD` en `.env`).

2. **Scheduler:** (Dispara las misiones)
   ```bash
   uv run python -m runtime.scheduler
   ```

3. **Worker:** (Procesa señales y detecta patrones)
   ```bash
   uv run python -m runtime.worker
   ```

## Production / Docker Runbook

Para desplegar RenderBrain en producción usando Docker, sigue estos pasos:

1. **Configurar secretos:**
   Copia el archivo de ejemplo y completa las contraseñas reales y API keys.
   ```bash
   cp .env.example .env
   # Edita .env con tus valores (ej: POSTGRES_PASSWORD, RENDERBRAIN_ADMIN_PASSWORD, etc.)
   ```

2. **Levantar bases de datos (Postgres y Redis):**
   ```bash
   docker compose up -d postgres redis
   ```

3. **Ejecutar migraciones (Estrategia de Migración):**
   Las migraciones deben ejecutarse como un paso previo explícito.
   ```bash
   # En local
   uv run alembic upgrade head
   
   # O mediante un contenedor efímero:
   # docker compose run --rm renderbrain-api uv run alembic upgrade head
   ```

4. **Levantar procesos de RenderBrain:**
   ```bash
   docker compose up -d renderbrain-api renderbrain-scheduler renderbrain-worker
   ```

Comandos individuales alternativos sin Docker:
- API: `uv run uvicorn runtime.api.main:app --host 0.0.0.0 --port 8000`
- Scheduler: `uv run python -m runtime.scheduler`
- Worker: `uv run python -m runtime.worker`
