"""
tests/integration/test_mission_persistence.py

Tests para la persistencia de Missions y la deduplicación vía ProcessedSignal (S4.1).
Valida contratos, repositorios y el constraint de unicidad en PostgreSQL.
"""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from runtime.contracts.mission import Mission
from runtime.contracts.processed_signal import ProcessedSignal
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.database.repositories.processed_signal import ProcessedSignalRepository


def test_mission_contract_validation():
    """Valida los chequeos básicos del contrato Mission."""
    with pytest.raises(ValueError, match="no puede estar vacío"):
        Mission(name="  ", source="src", target="tgt", interval_seconds=10)
    with pytest.raises(ValueError, match="mayor a 0"):
        Mission(name="N", source="S", target="T", interval_seconds=0)


def test_processed_signal_contract_validation():
    """Valida los chequeos básicos del contrato ProcessedSignal."""
    with pytest.raises(ValueError, match="no puede estar vacío"):
        ProcessedSignal(mission_id=uuid4(), source="", fingerprint="fg")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mission_repository_save_get_and_list_enabled():
    """Valida creación de Missions y filtrado por enabled=True."""
    mission_enabled = Mission(
        name="Test Enabled", source="test", target="test_target", interval_seconds=60, enabled=True
    )
    mission_disabled = Mission(
        name="Test Disabled", source="test", target="test_target", interval_seconds=60, enabled=False
    )

    async with async_session() as session:
        repo = MissionRepository(session)
        await repo.save(mission_enabled)
        await repo.save(mission_disabled)
        await session.commit()

        # Recuperar
        recovered = await repo.get_by_id(mission_enabled.id)
        assert recovered is not None
        assert recovered.name == "Test Enabled"

        # Listar habilitadas
        enabled_list = await repo.list_enabled()
        ids = [m.id for m in enabled_list]
        assert mission_enabled.id in ids
        assert mission_disabled.id not in ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_processed_signal_unique_constraint():
    """
    Valida el ciclo de deduplicación:
    1. Registro inicial.
    2. Comprobación vía exists().
    3. Falla estricta por constraint UNIQUE exacto.
    4. Permite mismo fingerprint en otra misión.
    5. Permite mismo fingerprint con otro source.
    """
    mission = Mission(name="Unique Test Mission", source="test", target="target", interval_seconds=60)
    sig_base = ProcessedSignal(mission_id=mission.id, source="test", fingerprint="hash123")

    async with async_session() as session:
        m_repo = MissionRepository(session)
        s_repo = ProcessedSignalRepository(session)

        # 1. Guardar inicial
        await m_repo.save(mission)
        await s_repo.add(sig_base)
        await session.commit()

        # 2. Exists
        assert await s_repo.exists(mission.id, "test", "hash123") is True
        assert await s_repo.exists(mission.id, "test", "other_hash") is False

        # 3. Constraint violation EXACTO
        sig_duplicate = ProcessedSignal(mission_id=mission.id, source="test", fingerprint="hash123")
        with pytest.raises(IntegrityError):
            await s_repo.add(sig_duplicate)
            await session.commit()

        await session.rollback()

        # 4. Diferente misión, mismo fingerprint (Permitido)
        other_mission = Mission(name="Other Mission", source="test", target="target", interval_seconds=60)
        sig_other_mission = ProcessedSignal(mission_id=other_mission.id, source="test", fingerprint="hash123")
        
        await m_repo.save(other_mission)
        await s_repo.add(sig_other_mission)
        await session.commit()

        # 5. Misma misión, diferente source (Permitido por el constraint de 3 columnas)
        sig_other_source = ProcessedSignal(mission_id=mission.id, source="other_src", fingerprint="hash123")
        await s_repo.add(sig_other_source)
        await session.commit()
