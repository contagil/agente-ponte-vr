"""
Cliente HTTP para o Reforma Legal — o lado "disca pra fora" da ponte.

A ponte nunca recebe conexão nenhuma (nem do RL, nem de ninguém). Ela puxa
trabalho via long-poll em /agente-vr/ponte/proximo-pedido e entrega o
resultado com um POST comum. Autenticação por chave compartilhada
(PONTE_API_KEY), separada da AGENTE_VR_API_KEY do cérebro.
"""
import os

import httpx

RL_BASE_URL = os.getenv("RL_BASE_URL", "http://localhost:8000/api").rstrip("/")
PONTE_API_KEY = os.getenv("PONTE_API_KEY", "")

# o long-poll do RL segura a conexão por AGENTE_VR_PONTE_LONG_POLL_S (~25s
# por padrão); o timeout do cliente precisa de folga sobre isso
LONG_POLL_HTTP_TIMEOUT_S = float(os.getenv("LONG_POLL_HTTP_TIMEOUT_S", "40"))
HTTP_TIMEOUT_S = float(os.getenv("RL_HTTP_TIMEOUT_S", "20"))


def _headers() -> dict[str, str]:
    if not PONTE_API_KEY:
        raise RuntimeError("PONTE_API_KEY ausente no .env da ponte")
    return {"Authorization": f"Bearer {PONTE_API_KEY}", "Content-Type": "application/json"}


async def proximo_pedido() -> dict | None:
    """Long-poll: bloqueia até um pedido aparecer ou o RL desistir e devolver
    corpo vazio. None = nada por agora, chame de novo."""
    async with httpx.AsyncClient(timeout=LONG_POLL_HTTP_TIMEOUT_S) as client:
        resp = await client.get(f"{RL_BASE_URL}/agente-vr/ponte/proximo-pedido", headers=_headers())
    resp.raise_for_status()
    if resp.status_code == 204 or not resp.content or resp.text.strip() in ("", "null"):
        return None
    return resp.json()


async def entregar_resultado(pedido_id: str, *, status: str, resumo: dict | None = None,
                             resultado: dict | None = None, erro: str | None = None) -> None:
    corpo = {"status": status, "resumo": resumo or {}, "resultado": resultado or {}, "erro": erro}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        resp = await client.post(
            f"{RL_BASE_URL}/agente-vr/ponte/resultado/{pedido_id}",
            headers=_headers(), json=corpo,
        )
    resp.raise_for_status()


async def enviar_heartbeat(diagnostico: dict) -> None:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        resp = await client.post(
            f"{RL_BASE_URL}/agente-vr/ponte/heartbeat", headers=_headers(), json=diagnostico,
        )
    resp.raise_for_status()
