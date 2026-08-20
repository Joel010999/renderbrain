FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd -m -U renderbrain

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /uvx /bin/

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

COPY . .

RUN chown -R renderbrain:renderbrain /app

USER renderbrain

ENTRYPOINT ["uv", "run"]
CMD ["uvicorn", "runtime.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
