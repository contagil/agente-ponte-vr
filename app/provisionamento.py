"""
Orquestração do provisionamento — cria cliente/agente no cérebro do agent-vr
e devolve o pacote de instalação pro Reforma Legal, pra eliminar o passo
manual do TI no console a cada cliente novo (PROPOSTA-PROVISIONAMENTO.md).

Mesmo espírito de sincronizacao.py: mora aqui porque é a ponte quem fala com
o cérebro; o RL só enfileira o pedido e recebe o resultado.
"""
import base64
import logging

from app.core import agente_vr_client as agente

log = logging.getLogger("ponte.provisionamento")


class ProvisionamentoError(Exception):
    """Falha em qualquer etapa do provisionamento — o pedido inteiro falha
    (não tem "análise parcial" aqui como na sincronização; cliente/agente ou
    saem criados e com pacote, ou o usuário refaz do zero)."""


async def provisionar_cliente_e_agente(
    client_id: str, nome_cliente: str, agent_id: str, tier: str = "test",
) -> dict:
    """Cria o cliente (tolera 409 — pode já existir de uma tentativa anterior
    que falhou depois), cria o agente (409 aqui É erro: reaproveitar um
    agent_id existente não é seguro, o enroll_token dele já foi consumido ou
    pertence a outro pedido) e baixa o pacote, pronto pra devolver ao RL como
    base64 (poucos MB — cabe no fluxo de resultado existente, PROPOSTA
    §5)."""
    if not agente.provisionamento_configurado():
        raise ProvisionamentoError(
            "Provisionamento não configurado nesta ponte "
            "(AGENTE_VR_PROVISIONAMENTO_API_KEY ausente no .env)."
        )

    try:
        await agente.provisionar_cliente(client_id, nome_cliente)
    except agente.AgenteVRError as exc:
        if exc.status_code != 409:
            raise ProvisionamentoError(f"falha ao criar cliente: {exc.mensagem}") from exc
        log.info("cliente %s já existia no agent-vr — seguindo com o agente", client_id)

    try:
        criado = await agente.provisionar_agente(agent_id, client_id, tier)
    except agente.AgenteVRError as exc:
        raise ProvisionamentoError(f"falha ao criar agente: {exc.mensagem}") from exc

    try:
        pacote_bytes = await agente.baixar_pacote_agente(agent_id)
    except agente.AgenteVRError as exc:
        # o agente já foi criado nesse ponto — não revoga sozinho: o usuário
        # ainda pode tentar baixar o pacote de novo (o token segue válido até
        # o primeiro enrollment), decisão de desfazer é manual.
        raise ProvisionamentoError(
            f"cliente e agente foram criados, mas falhou ao baixar o pacote: {exc.mensagem}"
        ) from exc

    return {
        "client_id": client_id,
        "agent_id": agent_id,
        "tier": tier,
        "enroll_token_emitido": bool(criado.get("enroll_token")),
        "pacote_zip_b64": base64.b64encode(pacote_bytes).decode("ascii"),
        "pacote_tamanho_bytes": len(pacote_bytes),
    }
