"""
runtime/contracts/mission.py

Contrato para la entidad Mission (Configuración Operacional pura).

A1.1 — Extensión para soporte de target_type=profile:
    - target_type: "post" | "profile"  (default "post" — retrocompatible)
    - observation_scope: clasificación del propósito de observación (opcional)
    - story_interval_seconds: intervalo dedicado para stories (solo profiles)
    - Normalización de target: acepta @username, URL completa o username limpio
      y los normaliza internamente a la forma canónica estable.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Constantes públicas
# ---------------------------------------------------------------------------

TARGET_TYPES = frozenset({"post", "profile"})
OBSERVATION_SCOPES = frozenset({"competitor", "inspiration", "market", "client", "reference"})

# Default de intervalo para misiones de perfil (24 horas)
DEFAULT_PROFILE_INTERVAL_SECONDS: int = 86400
# Default de intervalo para stories de perfil (6 horas — stories expiran en 24h)
DEFAULT_STORY_INTERVAL_SECONDS: int = 21600


# ---------------------------------------------------------------------------
# Helpers de normalización de username de Instagram
# ---------------------------------------------------------------------------

def normalize_instagram_username(raw: str) -> str:
    """
    Normaliza cualquier representación de un perfil de Instagram a un
    username limpio (sin @, sin URL, sin trailing slash).

    Formatos aceptados:
        "dimitris.tech"                             → "dimitris.tech"
        "@dimitris.tech"                            → "dimitris.tech"
        "https://instagram.com/dimitris.tech"       → "dimitris.tech"
        "https://www.instagram.com/dimitris.tech/"  → "dimitris.tech"

    Raises:
        ValueError: Si el resultado está vacío o contiene caracteres inválidos.
    """
    raw = raw.strip()

    # Si parece una URL, extraer el path
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        # Validar que sea de instagram.com
        host = parsed.netloc.lower().lstrip("www.")
        if not host.startswith("instagram.com"):
            raise ValueError(
                f"URL de perfil inválida: se esperaba instagram.com, recibido '{parsed.netloc}'."
            )
        # El path es "/username" o "/username/"
        path = parsed.path.strip("/")
        if not path or "/" in path:
            raise ValueError(
                f"URL de Instagram no apunta a un perfil de usuario: '{raw}'. "
                "Use el formato https://www.instagram.com/username"
            )
        username = path
    else:
        # Quitar @ inicial si existe
        username = raw.lstrip("@").strip()

    # Validar formato del username: letras, números, puntos, guiones bajos, máx 30 chars
    if not username:
        raise ValueError("El username de Instagram no puede estar vacío.")
    if len(username) > 30:
        raise ValueError(
            f"El username '{username}' excede los 30 caracteres permitidos por Instagram."
        )
    if not re.match(r"^[a-zA-Z0-9_.]+$", username):
        raise ValueError(
            f"Username de Instagram inválido: '{username}'. "
            "Solo se permiten letras, números, puntos y guiones bajos."
        )

    return username


class Mission(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    source: str
    target: str
    target_type: str = Field(
        default="post",
        description="Tipo de objetivo: 'post' (URL individual) | 'profile' (cuenta de Instagram).",
    )
    observation_scope: str | None = Field(
        default=None,
        description=(
            "Propósito de observación: 'competitor' | 'inspiration' | 'market' | 'client' | 'reference'. "
            "Solo aplica para target_type='profile'."
        ),
    )
    enabled: bool = True
    interval_seconds: int = Field(
        description=(
            "Intervalo de ejecución en segundos. "
            "Default 86400 (24h) para perfiles si no se especifica."
        ),
    )
    story_interval_seconds: int | None = Field(
        default=None,
        description=(
            "Intervalo dedicado para recolección de Stories (en segundos). "
            "Default 21600 (6h) para perfiles. Solo aplica si target_type='profile'."
        ),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
    last_collected_at: datetime | None = None

    @field_validator("name", "source", "target")
    @classmethod
    def validate_non_empty_strings(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"El campo '{info.field_name}' no puede estar vacío.")
        return v.strip()

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TARGET_TYPES:
            raise ValueError(
                f"target_type inválido: '{v}'. Valores permitidos: {sorted(TARGET_TYPES)}."
            )
        return v

    @field_validator("observation_scope")
    @classmethod
    def validate_observation_scope(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in OBSERVATION_SCOPES:
            raise ValueError(
                f"observation_scope inválido: '{v}'. "
                f"Valores permitidos: {sorted(OBSERVATION_SCOPES)}."
            )
        return v

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("interval_seconds debe ser mayor a 0.")
        return v

    @field_validator("story_interval_seconds")
    @classmethod
    def validate_story_interval(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("story_interval_seconds debe ser mayor a 0.")
        return v

    @model_validator(mode="after")
    def normalize_profile_target(self) -> "Mission":
        """
        Si target_type='profile', normaliza el target a username limpio.
        Si target_type='post', no modifica el target (se mantiene como URL).
        """
        if self.target_type == "profile":
            try:
                self.target = normalize_instagram_username(self.target)
            except ValueError as exc:
                raise ValueError(
                    f"El campo 'target' no es un perfil de Instagram válido: {exc}"
                ) from exc
        return self
