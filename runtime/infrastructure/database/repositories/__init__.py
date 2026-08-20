# runtime/infrastructure/database/repositories
#
# Importar desde aquí:
#   from runtime.infrastructure.database.repositories import CanonicalSignalRepository

from runtime.infrastructure.database.repositories.canonical_signal import CanonicalSignalRepository
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository

__all__ = ["CanonicalSignalRepository", "KnowledgeCoreRepository"]
