"""
runtime/api/auth.py

Implementación mínima de seguridad para proteger rutas y el Dashboard.
Utiliza HTTP Basic Auth.
"""
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from runtime.shared.config import settings

security = HTTPBasic()

def get_current_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """
    Verifica las credenciales provistas contra la configuración (environment).
    """
    if not settings.RENDERBRAIN_ADMIN_USERNAME or not settings.RENDERBRAIN_ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Las credenciales administrativas no están configuradas. Proteja el entorno asignando RENDERBRAIN_ADMIN_USERNAME y RENDERBRAIN_ADMIN_PASSWORD.",
        )

    correct_username = secrets.compare_digest(credentials.username, settings.RENDERBRAIN_ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, settings.RENDERBRAIN_ADMIN_PASSWORD.get_secret_value())

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
