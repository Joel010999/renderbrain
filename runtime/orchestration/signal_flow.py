"""
runtime/orchestration/signal_flow.py

Orquestador mínimo del First Signal Flow — S1.4.

Responsabilidad:
    Conectar las capas del flujo vertical sin introducir lógica de dominio:

        EventEnvelope (leído de Redis Stream)
            → RawSignalDetected (reconstruido desde el payload)
            → NormalizerEngine.normalize()                  (señal canónica base)
            → model_copy(source_event_id=envelope.event_id) (trazabilidad real)
            → CanonicalSignalRepository.save()              (persistencia)

    Devuelve el CanonicalSignal persistido para que el llamador pueda
    verificar la trazabilidad (útil en tests y futuros workers).

Principios de diseño:
- El orquestador es el único punto que conoce Redis, PostgreSQL y EventEnvelope.
- NormalizerEngine permanece puro: no recibe el envelope ni conoce su ID.
- La asignación de source_event_id ocurre aquí, usando model_copy() para
  respetar la inmutabilidad de los contratos Pydantic.
- Sin consumer groups, retries, DLQ ni lógica de workers en este MVP.
- La sesión SQLAlchemy y el cliente Redis son inyectados por el llamador.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.contracts.canonical_signal import CanonicalSignal, CanonicalSignalData
from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.engines.normalizer import NormalizerEngine
from runtime.infrastructure.database.repositories import CanonicalSignalRepository


async def run_signal_flow(
    envelope: EventEnvelope,
    session: AsyncSession,
) -> CanonicalSignal:
    """
    Ejecuta el flujo completo desde un EventEnvelope hasta la persistencia.

    Pasos internos:
        1. Reconstruir RawSignalDetected desde envelope.payload.
        2. Normalizar con NormalizerEngine (produce un CanonicalSignalData).
        3. Asignar trazabilidad instanciando CanonicalSignal con source_event_id = envelope.event_id.
        4. Persistir vía CanonicalSignalRepository.

    Args:
        envelope: EventEnvelope leído desde el Redis Stream.
        session:  AsyncSession activa. El llamador gestiona commit/rollback
                  y el ciclo de vida de la sesión.

    Returns:
        CanonicalSignal persistido con trazabilidad completa.
    """
    # 1. Reconstruir RawSignalDetected desde el payload del envelope
    raw_signal = RawSignalDetected.model_validate(envelope.payload)

    # 2. Normalizar — NormalizerEngine produce CanonicalSignalData puro.
    engine = NormalizerEngine()
    canonical_data: CanonicalSignalData = await engine.normalize(raw_signal)

    # 3. Asignar trazabilidad real instanciando el contrato final
    canonical = CanonicalSignal(
        **canonical_data.model_dump(),
        source_event_id=envelope.event_id
    )

    # 4. Persistir
    repo = CanonicalSignalRepository(session)
    await repo.save(canonical)

    return canonical
