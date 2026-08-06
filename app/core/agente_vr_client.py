"""
Cliente da API de leitura externa do Agente VR (cérebro em `agents.contagilpb.com.br`).

O Reforma Legal entra nessa integração como *aplicação consumidora*: autentica
com uma API key própria (`Authorization: Bearer`), só pode invocar os `task_id`
que estão no escopo concedido a ela e **nunca envia SQL** — o catálogo de
tarefas é gerido pelo console do agent-vr, fora deste projeto. Ver
`docs/agente-vr-catalogo-tarefas.md` para o SQL que precisa estar cadastrado lá.

Por que passar pelo cérebro em vez de conectar direto no Postgres do cliente:
o banco do VR fica atrás de NAT/VPN na rede do supermercado; quem alcança o
banco é o agente, que disca pra fora. O RL nunca vê credencial de cliente.
"""
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

API_URL = os.getenv("AGENTE_VR_API_URL", "https://agents.contagilpb.com.br/api/v1")
API_KEY = os.getenv("AGENTE_VR_API_KEY", "")

# O cérebro espera até o timeout_ms da tarefa + 15s de margem antes de devolver
# 504; o client HTTP daqui precisa ser mais tolerante que isso, senão a gente
# desiste antes dele responder (PROPOSTA-API-EXTERNA §7).
TIMEOUT_S = float(os.getenv("AGENTE_VR_TIMEOUT_S", "180"))


class AgenteVRError(Exception):
    """Falha na conversa com o cérebro, já traduzida para o RL.

    `status_code` é o código HTTP que o endpoint do RL deve devolver — os
    códigos do cérebro são repassados de propósito (503 = agente offline,
    504 = tempo esgotado, 429 = concorrência, 409 = mais de um agente online),
    porque cada um deles pede uma orientação diferente na tela.
    """

    def __init__(self, status_code: int, mensagem: str):
        super().__init__(mensagem)
        self.status_code = status_code
        self.mensagem = mensagem


@dataclass
class ResultadoConsulta:
    """Retorno padronizado de qualquer modo da API externa.

    Atenção: o cérebro relê o resultado de um CSV, então **toda célula chega
    como string** (ou string vazia para NULL). Quem consome precisa coagir os
    tipos — ver os helpers de `analise_vr.py`.
    """

    columns: list[str]
    rows: list[list[str]]
    total: int = 0
    executado_em: str | None = None
    consulta_id: str | None = None
    task_id: str | None = None
    client_id: str | None = None
    agent_id: str | None = None
    bruto: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def de_payload(cls, payload: dict) -> "ResultadoConsulta":
        return cls(
            columns=payload.get("columns") or [],
            rows=payload.get("rows") or [],
            total=payload.get("total") or 0,
            executado_em=payload.get("executado_em"),
            consulta_id=payload.get("consulta_id"),
            task_id=payload.get("task_id"),
            client_id=payload.get("client_id"),
            agent_id=payload.get("agent_id"),
            bruto=payload,
        )

    def como_dicts(self) -> list[dict[str, str]]:
        """Linhas indexadas pelo nome da coluna, normalizado para minúsculas —
        o cabeçalho vem do alias usado no SQL cadastrado no console, e não dá
        para garantir a caixa de quem cadastrou."""
        chaves = [str(c).strip().lower() for c in self.columns]
        return [dict(zip(chaves, linha)) for linha in self.rows]


def _headers() -> dict[str, str]:
    if not API_KEY:
        raise AgenteVRError(
            503,
            "Integração com o Agente VR não configurada neste servidor "
            "(AGENTE_VR_API_KEY ausente).",
        )
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _traduzir_erro(resp: httpx.Response) -> AgenteVRError:
    """Erros de runtime do cérebro vêm como {status, error}; erros de validação
    (escopo, params, cliente inexistente) vêm no {detail} padrão do FastAPI."""
    try:
        corpo = resp.json()
    except ValueError:
        corpo = {}
    msg = corpo.get("error") or corpo.get("detail") or resp.text or "falha na consulta"
    if isinstance(msg, list):  # erro de validação do próprio FastAPI
        msg = "; ".join(str(m.get("msg", m)) for m in msg)
    return AgenteVRError(resp.status_code, str(msg))


async def _get(caminho: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(f"{API_URL}{caminho}", headers=_headers(), params=params)
    except httpx.RequestError as exc:
        raise AgenteVRError(502, f"não foi possível falar com o Agente VR: {exc}") from exc
    if resp.status_code >= 400:
        raise _traduzir_erro(resp)
    return resp.json()


async def _post(caminho: str, corpo: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(f"{API_URL}{caminho}", headers=_headers(), json=corpo)
    except httpx.ReadTimeout as exc:
        raise AgenteVRError(
            504,
            "o agente não respondeu no tempo esperado; se a consulta concluir, "
            "o resultado fica disponível na próxima sincronização",
        ) from exc
    except httpx.RequestError as exc:
        raise AgenteVRError(502, f"não foi possível falar com o Agente VR: {exc}") from exc
    if resp.status_code >= 400:
        raise _traduzir_erro(resp)
    return resp.json()


def integracao_configurada() -> bool:
    """Permite a tela distinguir 'servidor sem integração' de 'agente offline'."""
    return bool(API_KEY)


async def whoami() -> dict:
    """Sanidade da integração: confirma a chave e devolve o escopo concedido."""
    return await _get("/whoami")


async def consultar(
    task_id: str,
    client_id: str,
    params: dict | None = None,
    agent_id: str | None = None,
) -> ResultadoConsulta:
    """Modo síncrono (§4.1): dispara a tarefa no agente do cliente e espera o dado."""
    corpo: dict[str, Any] = {
        "task_id": task_id,
        "client_id": client_id,
        "params": params or {},
    }
    if agent_id:
        corpo["agent_id"] = agent_id
    return ResultadoConsulta.de_payload(await _post("/consultas", corpo))


async def ultimo_resultado(
    task_id: str, client_id: str, max_age_h: float | None = None
) -> ResultadoConsulta | None:
    """Modo 'último resultado pronto' (§4.3): não dispara nada.

    Devolve None quando nunca rodou / está velho demais / o CSV já foi apagado
    pela retenção — os três são "não tenho dado pra mostrar", e a tela trata
    igual (oferece sincronizar).
    """
    params: dict[str, Any] = {"client_id": client_id}
    if max_age_h is not None:
        params["max_age_h"] = max_age_h
    try:
        return ResultadoConsulta.de_payload(await _get(f"/consultas/{task_id}/ultimo", params))
    except AgenteVRError as exc:
        if exc.status_code in (404, 410):
            return None
        raise
