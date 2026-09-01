# runtime/infrastructure/database/models
#
# Importar este package registra todos los modelos ORM en Base.metadata,
# lo que permite que Alembic autogenerate los detecte correctamente.
#
# Importar desde aquí:
#   from runtime.infrastructure.database.models import CanonicalSignalModel

from runtime.infrastructure.database.session import Base
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.models.knowledge import (
    EvidenceModel,
    InsightModel,
    KnowledgeTransactionModel,
)
from runtime.infrastructure.database.models.mission import MissionModel, ProcessedSignalModel
from runtime.infrastructure.database.models.content_brief import ContentBriefModel

__all__ = [
    "Base",
    "CanonicalSignalModel",
    "ContentBriefModel",
    "EvidenceModel",
    "InsightModel",
    "KnowledgeTransactionModel",
    "MissionModel",
    "ProcessedSignalModel",
]
