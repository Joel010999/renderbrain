"""
runtime/workers/signal_worker.py

SignalWorker — Worker real de RenderBrain con deduplicación (S4.3).

Responsabilidad:
    Consumir EventEnvelopes desde un Redis Stream vía Consumer Group, deduplicar
    por fingerprint y ejecutar secuencialmente los dos flows del pipeline:

        Redis Stream
            → EventEnvelope
            → compute_fingerprint()         — extrae identidad estable
            → ProcessedSignalRepository.exists() — dedupe check
                → HIT (duplicado): XACK inmediato, sin LLM, sin normalización
                → MISS (nuevo): continúa el pipeline
            → run_signal_flow()             → CanonicalSignal (flush)
            → ProcessedSignalRepository.add() → flush dentro de la misma sesión
            → run_cognitive_flow()          → KnowledgeTransaction | None
                                              (commit interno si relevant=True,
                                               commit del worker si relevant=False)
            → commit durable (incluye CanonicalSignal + ProcessedSignal + Knowledge)
            → XACK ✅

Límites Transaccionales:
    - Una única AsyncSession por mensaje procesado.
    - El dedupe check (exists) se hace DENTRO de la sesión pero SIN flush previo.
    - run_signal_flow()             → flush() del CanonicalSignal (staging).
    - ProcessedSignalRepository.add() → flush() del ProcessedSignal (staging).
    - run_cognitive_flow() relevant=True  → session.commit() interno (CanonicalSignal
                                            + ProcessedSignal + Knowledge en un commit).
    - run_cognitive_flow() relevant=False → retorna None, worker hace session.commit()
                                            (CanonicalSignal + ProcessedSignal).
    - IntegrityError (race condition ganado por concurrent) → rollback + XACK.
    - Cualquier otra excepción → session.rollback() + re-raise → NO XACK.
    - XACK ocurre ÚNICAMENTE después de commit exitoso o hit de deduplicación.

Crash Recovery:
    Si el worker hace commit pero muere antes del XACK:
    - XAUTOCLAIM recupera el mensaje de la PEL.
    - exists() retorna True (ProcessedSignal ya en BD) → HIT → XACK sin LLM.
    - Sin doble procesamiento, sin tokens LLM desperdiciados.

Separación de IDs:
    - redis_entry_id: ID técnico de Redis, se usa en XACK.
    - envelope.event_id: UUID de RenderBrain → source_event_id en CanonicalSignal.
    - fingerprint: identidad de negocio del post (ej. "instagram:CTest12345").
"""

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.knowledge import KnowledgeTransaction
from runtime.contracts.processed_signal import ProcessedSignal
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.events.consumer_group import RedisConsumerGroup
from runtime.infrastructure.database.repositories.processed_signal import (
    ProcessedSignalRepository,
)
from runtime.orchestration.cognitive_flow import run_cognitive_flow
from runtime.orchestration.signal_flow import run_signal_flow
from runtime.shared.logger import get_logger
from runtime.workers.fingerprint import FingerprintError, compute_fingerprint

logger: logging.Logger = get_logger(__name__)

# Sentinel para indicar que un mensaje fue omitido por deduplicación.
# Se usa como segundo elemento de la tupla de retorno.
_DUPLICATE = "duplicate"


class SignalWorker:
    """
    Worker de procesamiento de señales con deduplicación efectiva.

    Garantiza "at-least-once delivery" con "effectively-once processing":
    - Redis Consumer Groups + PEL: at-least-once a nivel de transporte.
    - ProcessedSignal + UNIQUE constraint: effectively-once a nivel de negocio.

    Diseñado para ser testeable mediante process_one() — sin loops internos.

    Args:
        consumer_group:   RedisConsumerGroup con stream/group/consumer configurados.
        session_factory:  async_sessionmaker directo (NO get_session()) para
                          control manual de commit/rollback.
        cognitive_engine: CognitiveEngine con LLMProvider inyectado.
        llm_provider:     LLMProvider usado explícitamente para construir detectores.
        mission_context:  Contexto de misión para el análisis cognitivo.
                          Inyectado externamente; no se hace retrieval de DB aquí.
    """

    def __init__(
        self,
        consumer_group: RedisConsumerGroup,
        session_factory: async_sessionmaker[AsyncSession],
        cognitive_engine: CognitiveEngine,
        llm_provider: LLMProvider,
        mission_context: str,
    ) -> None:
        self._group = consumer_group
        self._session_factory = session_factory
        self._cognitive_engine = cognitive_engine
        self._llm_provider = llm_provider
        self._mission_context = mission_context

    # ------------------------------------------------------------------
    # API pública — determinista y testeable
    # ------------------------------------------------------------------

    async def process_one(
        self,
        entry_id: str,
        envelope: EventEnvelope,
    ) -> tuple[CanonicalSignal | None, KnowledgeTransaction | None]:
        """
        Procesa exactamente un mensaje de forma determinista.

        Retorno:
            (canonical, transaction) — procesado normalmente.
            (None, None)             — duplicado o race condition perdida.

        El XACK ocurre en AMBOS casos (duplicado y nuevo procesado).
        NO se hace XACK únicamente si hay excepción no controlada.

        Args:
            entry_id: Redis Entry ID (distinto del envelope.event_id).
            envelope: EventEnvelope a procesar.

        Raises:
            FingerprintError: Si el payload no tiene identidad estable.
                              El mensaje queda pending (sin XACK).
            Exception:        Cualquier error de normalización, DB o LLM.
                              El mensaje queda pending (sin XACK).
        """
        logger.info(
            "Processing message",
            extra={
                "redis_entry_id": entry_id,
                "event_id": str(envelope.event_id),
                "event_type": envelope.event_type,
            },
        )

        # ------------------------------------------------------------------
        # Paso 0: Reconstruir RawSignalDetected y calcular fingerprint.
        # Esto ocurre ANTES de abrir la sesión de BD para fallar rápido
        # si el payload no tiene identidad estable — sin consumir conexiones.
        # FingerprintError se propaga como excepción → NO XACK.
        # ------------------------------------------------------------------
        raw_signal = RawSignalDetected.model_validate(envelope.payload)
        fingerprint = compute_fingerprint(raw_signal)

        logger.debug(
            "Fingerprint computed",
            extra={
                "fingerprint": fingerprint,
                "source": raw_signal.source,
                "event_id": str(envelope.event_id),
            },
        )

        canonical: CanonicalSignal | None = None
        transaction: KnowledgeTransaction | None = None
        _is_duplicate = False  # control flag: True = hit or race condition

        async with self._session_factory() as session:
            try:
                # ----------------------------------------------------------
                # Paso 1: Dedupe check — ANTES de normalizar o llamar al LLM.
                # Si ya existe este fingerprint para esta misión+fuente,
                # es un duplicado: XACK inmediato sin procesamiento.
                # ----------------------------------------------------------
                dedupe_repo = ProcessedSignalRepository(session)
                already_processed = await dedupe_repo.exists(
                    mission_id=raw_signal.mission_id,
                    source=raw_signal.source,
                    fingerprint=fingerprint,
                )

                if already_processed:
                    logger.info(
                        "Duplicate signal detected — skipping processing",
                        extra={
                            "fingerprint": fingerprint,
                            "source": raw_signal.source,
                            "event_id": str(envelope.event_id),
                        },
                    )
                    # Salida limpia: no se modificó nada en la sesión.
                    # Marcamos como duplicado; el XACK se ejecuta abajo.
                    _is_duplicate = True

                if not _is_duplicate:
                    # ----------------------------------------------------------
                    # Paso 2: Signal Flow — normaliza y hace flush (no commit).
                    # ----------------------------------------------------------
                    canonical = await run_signal_flow(envelope, session)
                    logger.debug(
                        "Signal flow completed",
                        extra={
                            "canonical_signal_id": str(canonical.id),
                            "source_event_id": str(canonical.source_event_id),
                        },
                    )

                    # ----------------------------------------------------------
                    # Paso 3: Registrar ProcessedSignal ANTES de cognitive_flow.
                    #
                    # CRÍTICO: run_cognitive_flow() hace session.commit() si
                    # relevant=True. Si agregamos ProcessedSignal DESPUÉS,
                    # quedaría sin commitear o necesitaríamos un segundo commit.
                    #
                    # Al hacerlo AQUÍ (flush sin commit), el commit de
                    # cognitive_flow incluye atómicamente:
                    #   CanonicalSignal + ProcessedSignal + Evidence + Insight + KnowledgeTx
                    #
                    # Para relevant=False, el session.commit() del worker
                    # incluye: CanonicalSignal + ProcessedSignal.
                    # ----------------------------------------------------------
                    processed_record = ProcessedSignal(
                        mission_id=raw_signal.mission_id,
                        source=raw_signal.source,
                        fingerprint=fingerprint,
                    )
                    await dedupe_repo.add(processed_record)
                    logger.debug(
                        "ProcessedSignal staged",
                        extra={
                            "fingerprint": fingerprint,
                            "processed_signal_id": str(processed_record.id),
                        },
                    )

                    # ----------------------------------------------------------
                    # Paso 4: Cognitive Flow
                    #
                    # run_cognitive_flow hace flush. 
                    # ----------------------------------------------------------
                    from runtime.engines.cognitive.retriever import KnowledgeContextRetriever
                    from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
                    from runtime.engines.cognitive.pattern_detector import (
                        PatternDetector,
                        InvalidPatternOutputError,
                    )
                    
                    repo = KnowledgeCoreRepository(session)
                    retriever = KnowledgeContextRetriever(repository=repo)
                    
                    intelligence_view = await retriever.retrieve(canonical.mission_id)
                    
                    transaction = await run_cognitive_flow(
                        signal=canonical,
                        mission_context=self._mission_context,
                        cognitive_engine=self._cognitive_engine,
                        intelligence_view=intelligence_view,
                        session=session,
                    )

                    if transaction is not None:
                        # Refrescar la vista para incluir el nuevo insight antes de Pattern Detection
                        intelligence_view = await retriever.retrieve(canonical.mission_id)
                        
                        # ----------------------------------------------------------
                        # Paso 5: Pattern Detection
                        # ----------------------------------------------------------
                        pattern_detector = PatternDetector(llm_provider=self._llm_provider)
                        
                        try:
                            pattern, supporting_ids = await pattern_detector.detect(
                                mission_id=canonical.mission_id,
                                mission_context=self._mission_context,
                                intelligence_view=intelligence_view,
                            )
                        except InvalidPatternOutputError as pat_err:
                            logger.warning(
                                "Invalid pattern output — discarding pattern, preserving valid intelligence",
                                extra={
                                    "error": str(pat_err),
                                    "fingerprint": fingerprint,
                                    "canonical_signal_id": str(canonical.id),
                                }
                            )
                            pattern = None
                            supporting_ids = []
                        
                        if pattern:
                            await repo.add_pattern(pattern, supporting_ids)
                            logger.info(
                                "Pattern detected and staged",
                                extra={
                                    "pattern_id": str(pattern.id),
                                    "support_count": pattern.support_count,
                                }
                            )

                            # Refrescar la vista de inteligencia para incluir el patrón recién detectado
                            intelligence_view = await retriever.retrieve(canonical.mission_id)

                        # ----------------------------------------------------------
                        # Paso 6: Opportunity Detection
                        #
                        # Degradación controlada: InvalidOpportunitySupportError indica
                        # que el JSON del LLM fue parseado pero los índices de soporte
                        # son semánticamente inválidos (ej: índice fuera de rango).
                        # En este caso descartamos únicamente la Opportunity y dejamos
                        # continuar el pipeline — Insight, Pattern y ProcessedSignal
                        # se persisten normalmente en el commit final.
                        #
                        # Errores de infraestructura (timeout, RuntimeError, DB) siguen
                        # siendo fatales y se propagan al except Exception externo.
                        # ----------------------------------------------------------
                        from runtime.engines.cognitive.opportunity_detector import (
                            OpportunityDetector,
                            InvalidOpportunitySupportError,
                        )
                        opportunity_detector = OpportunityDetector(llm_provider=self._llm_provider)

                        try:
                            opportunity, opp_supporting_ids = await opportunity_detector.detect(
                                mission_id=canonical.mission_id,
                                mission_context=self._mission_context,
                                intelligence_view=intelligence_view,
                            )
                        except InvalidOpportunitySupportError as opp_err:
                            logger.warning(
                                "Invalid opportunity support indexes — discarding opportunity, preserving valid intelligence",
                                extra={
                                    "error": str(opp_err),
                                    "fingerprint": fingerprint,
                                    "canonical_signal_id": str(canonical.id),
                                },
                            )
                            opportunity = None
                            opp_supporting_ids = []

                        if opportunity:
                            await repo.add_opportunity(opportunity, opp_supporting_ids)
                            logger.info(
                                "Opportunity detected and staged",
                                extra={
                                    "opportunity_id": str(opportunity.id),
                                    "support_count": len(opp_supporting_ids),
                                }
                            )

                    # ----------------------------------------------------------
                    # PASO FINAL: Commit Atómico Conjunto
                    # Todo lo que se hizo (CanonicalSignal, Evidence, Insight, 
                    # ProcessedSignal, Pattern, y Opportunity) queda durable al unísono.
                    # ----------------------------------------------------------
                    await session.commit()

                    if transaction is None:
                        logger.info(
                            "Signal not relevant — CanonicalSignal + ProcessedSignal persisted",
                            extra={
                                "canonical_signal_id": str(canonical.id),
                                "fingerprint": fingerprint,
                            },
                        )
                    else:
                        logger.info(
                            "Cognitive flow completed — full pipeline persisted",
                            extra={
                                "canonical_signal_id": str(canonical.id),
                                "transaction_id": str(transaction.id),
                                "fingerprint": fingerprint,
                            },
                        )


            except IntegrityError as e:
                # Race condition: otro worker committeó el mismo fingerprint
                # antes que nosotros. El UNIQUE constraint lo interceptó.
                await session.rollback()

                # Solo tratamos como duplicate si la violación es exactamente
                # el constraint de deduplicación (uq_processed_signal).
                # Fallback: otros IntegrityError (FK, NOT NULL) deben propagarse.
                is_duplicate_constraint = False
                if hasattr(e.orig, "__cause__"):
                    orig_cause = e.orig.__cause__
                    if getattr(orig_cause, "constraint_name", None) == "uq_processed_signal":
                        is_duplicate_constraint = True

                if is_duplicate_constraint:
                    logger.info(
                        "Race condition detected — concurrent worker already processed this signal",
                        extra={
                            "fingerprint": fingerprint,
                            "entry_id": entry_id,
                        },
                    )
                    # Marcamos como duplicado efectivo (concurrent ganó).
                    # El mensaje será XACK'd y NO se llama al LLM.
                    _is_duplicate = True
                    canonical = None
                    transaction = None
                else:
                    logger.warning(
                        "Unexpected IntegrityError — rolling back session, message stays pending",
                        extra={
                            "redis_entry_id": entry_id,
                            "event_id": str(envelope.event_id),
                            "fingerprint": fingerprint,
                        },
                        exc_info=True,
                    )
                    raise  # NO XACK — el mensaje queda pending para reproceso


            except Exception:
                # Fallo real (normalizer, LLM, DB no-integrity) → rollback + NO XACK.
                await session.rollback()
                logger.warning(
                    "Processing failed — rolling back session, message stays pending",
                    extra={
                        "redis_entry_id": entry_id,
                        "event_id": str(envelope.event_id),
                        "fingerprint": fingerprint,
                    },
                    exc_info=True,
                )
                raise  # NO XACK — el mensaje queda pending para reproceso

        # ------------------------------------------------------------------
        # XACK — fuera del bloque de sesión, solo si no hubo excepción.
        # Se ejecuta tanto para: nuevo procesado | duplicado | race condition.
        # NO se ejecuta si hubo excepción (raise sale del método antes de aquí).
        # ------------------------------------------------------------------
        await self._group.ack(entry_id)
        logger.info(
            "Message acknowledged (XACK)",
            extra={
                "redis_entry_id": entry_id,
                "event_id": str(envelope.event_id),
            },
        )

        return canonical, transaction

    async def process_next(
        self,
        count: int = 1,
    ) -> list[tuple[CanonicalSignal | None, KnowledgeTransaction | None]]:
        """
        Lee y procesa los próximos `count` mensajes nuevos del stream.

        Primero reclama mensajes pending (PEL) — incluye mensajes que fallaron
        en iteraciones previas y mensajes donde el worker murió antes del XACK
        (crash recovery via XAUTOCLAIM).

        Luego lee mensajes nuevos sin bloquear.

        Args:
            count: Máximo de mensajes a procesar.

        Returns:
            Lista de resultados de process_one(). (None, None) para duplicados.
        """
        results: list[tuple[CanonicalSignal | None, KnowledgeTransaction | None]] = []

        # 1. Pending primero (crash recovery + reproceso de fallos anteriores)
        pending = await self._group.read_pending(count=count, min_idle_ms=0)
        for entry_id, envelope in pending:
            try:
                result = await self.process_one(entry_id, envelope)
                results.append(result)
            except Exception as e:
                logger.error("Error processing pending message, continuing with next", exc_info=True, extra={"entry_id": entry_id, "error": str(e)})

        # 2. Mensajes nuevos
        remaining = count - len(results)
        if remaining > 0:
            new_messages = await self._group.read_new(count=remaining, block_ms=None)
            for entry_id, envelope in new_messages:
                try:
                    result = await self.process_one(entry_id, envelope)
                    results.append(result)
                except Exception as e:
                    logger.error("Error processing new message, continuing with next", exc_info=True, extra={"entry_id": entry_id, "error": str(e)})

        return results
