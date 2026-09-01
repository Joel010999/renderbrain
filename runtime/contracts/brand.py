"""
runtime/contracts/brand.py

Contexto de marca persistido/configurable mínimo para Agent 3 (Content Strategist).
Define explícitamente quién es la marca (BRAND) que genera el contenido, para
evitar que el Agente asuma la identidad de la cuenta observada (SOURCE).
"""

from typing import Dict, Any

BRAND_CONTEXT: Dict[str, Any] = {
    "brand_name": "RenderByte",
    "brand_description": "Empresa de software, automatización, IA y soluciones digitales para negocios.",
    "core_services": [
        "sistemas internos de gestión",
        "stock, ventas y cobranzas",
        "CRM",
        "automatizaciones",
        "integración de IA",
        "páginas web",
        "ecommerce"
    ],
    "target_audience": (
        "Dueños de negocios, PyMEs y empresas que todavía manejan procesos manuales, "
        "Excel, WhatsApp o herramientas desconectadas."
    ),
    "content_goal": "Generar autoridad, awareness y oportunidades comerciales calificadas."
}
