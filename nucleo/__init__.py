"""Núcleo do servidor de vídeos: configuração, banco, armazenamento, provedores, agentes e pipeline.

Veja docs/arquitetura.md: é o contrato entre as camadas.
"""

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
