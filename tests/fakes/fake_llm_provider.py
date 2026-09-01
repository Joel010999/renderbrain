"""
tests/fakes/fake_llm_provider.py

Fake provider para tests offline del LLM (S3.2).
"""

from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.infrastructure.llm.errors import LLMInputError


class FakeLLMProvider(LLMProvider):
    """
    Simula un LLMProvider devolviendo una respuesta predefinida.
    Ahora soporta múltiples fases chequeando el prompt.
    """
    def __init__(self, predefined_response: str = "Fake response", abstraction_response: str = None) -> None:
        self.predefined_response = predefined_response
        self.abstraction_response = abstraction_response
        self.call_count = 0
        self.last_prompts = []

    async def complete(self, prompt: str) -> str:
        self.call_count += 1
        self.last_prompts.append(prompt)
        
        if not prompt or not prompt.strip():
            raise LLMInputError("El prompt no puede estar vacío.")
            
        if "QA Reviewer" in prompt:
            return '{"is_aligned": true, "reason": "ok"}'
            
        if "Fase A: Abstracción" in prompt:
            if self.abstraction_response:
                return self.abstraction_response
            # Default fallback for abstraction if not explicitly provided
            # Parses self.predefined_response to extract abstraction fields if possible
            import json
            try:
                data = json.loads(self.predefined_response)
                return json.dumps({
                    "transferable_insight": data.get("transferable_insight", "Fake insight"),
                    "brand_service_alignment": data.get("brand_service_alignment", "crm"),
                    "business_pain": "Fake pain",
                    "rationale": "Fake rationale"
                })
            except:
                return '{"transferable_insight": "Fake insight", "brand_service_alignment": "crm", "business_pain": "pain", "rationale": "rat"}'
                
        # By default (or for Fase B), return the predefined_response
        return self.predefined_response
