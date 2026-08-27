"""
Ponte do Agente VR — processo cliente puro, roda dentro da rede da Contágil.

Não expõe porta nenhuma. Só disca pra fora, em dois sentidos:
  - pro cérebro do agent-vr (10.0.100.204:8040, rede interna) — sob demanda,
    quando há um pedido pra processar;
  - pro Reforma Legal (externo) — via long-poll, puxando trabalho, e depois
    entregando o resultado com um POST comum.

Nenhuma conexão de fora chega até aqui. Mesmo princípio do próprio agente do
agent-vr, um nível acima. Ver docs/ACESSO-API-APP-EXTERNA.md (no repo do
agent-vr) para o porquê desse desenho.

Uso:
    python -m app.main
"""
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from app import cliente_rl  # noqa: E402
from app.sincronizacao import SincronizacaoError, diagnostico, sincronizar_cliente  # noqa: E402
from app.provisionamento import ProvisionamentoError, provisionar_cliente_e_agente  # noqa: E402
from app.core import agente_vr_client as agente  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ponte")

# o heartbeat rico (escopo, base RFB) não precisa ir a cada poll — o poll em
# si já prova que a ponte está viva; isto só atualiza o diagnóstico
HEARTBEAT_INTERVAL_S = float(os.getenv("HEARTBEAT_INTERVAL_S", "60"))

# depois de uma falha de rede (RL ou cérebro fora do ar), espera antes de
# tentar de novo — evita martelar em loop apertado
RETRY_BACKOFF_S = float(os.getenv("RETRY_BACKOFF_S", "5"))


async def _processar_sincronizacao(pedido: dict) -> None:
    pedido_id = pedido["pedido_id"]
    client_id = pedido["client_id"]
    agent_id = pedido.get("agent_id")
    cclasstrib_lista = pedido.get("cclasstrib_lista") or []

    log.info("processando pedido %s (sincronizar, client_id=%s)", pedido_id, client_id)
    inicio = time.monotonic()
    try:
        resultado = await sincronizar_cliente(client_id, agent_id, cclasstrib_lista)
    except SincronizacaoError as exc:
        log.warning("pedido %s falhou: %s", pedido_id, exc)
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada também vira "erro", não crash
        log.exception("pedido %s: falha inesperada", pedido_id)
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"erro interno: {exc}")
        return

    from app.core import analise_vr
    resumo = analise_vr.resumir(resultado)
    await cliente_rl.entregar_resultado(pedido_id, status="concluido", resumo=resumo, resultado=resultado)
    log.info("pedido %s concluído em %.1fs", pedido_id, time.monotonic() - inicio)


async def _processar_provisionamento(pedido: dict) -> None:
    pedido_id = pedido["pedido_id"]
    client_id = pedido["client_id"]
    nome_cliente = pedido.get("nome_cliente") or client_id
    agent_id = pedido.get("agent_id")
    tier = pedido.get("tier") or "test"

    log.info("processando pedido %s (provisionar, client_id=%s, agent_id=%s)",
             pedido_id, client_id, agent_id)
    inicio = time.monotonic()
    try:
        resultado = await provisionar_cliente_e_agente(client_id, nome_cliente, agent_id, tier)
    except ProvisionamentoError as exc:
        log.warning("pedido %s falhou: %s", pedido_id, exc)
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("pedido %s: falha inesperada", pedido_id)
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"erro interno: {exc}")
        return

    await cliente_rl.entregar_resultado(pedido_id, status="concluido", resumo={}, resultado=resultado)
    log.info("pedido %s (provisionar) concluído em %.1fs", pedido_id, time.monotonic() - inicio)


async def _processar_status_agente(pedido: dict) -> None:
    """RASCUNHO (PROPOSTA-PROVISIONAMENTO.md §3.4, branch draft/api-db-config-rl
    do agent-vr — ainda não revisada). O RL usa isto pra saber quando o agente
    já fez enrollment (libera a etapa de configurar banco) e pra acompanhar o
    resultado da última tentativa de conexão (db_status/db_error)."""
    pedido_id = pedido["pedido_id"]
    agent_id = pedido.get("agent_id")
    try:
        status = await agente.status_agente(agent_id)
    except agente.AgenteVRError as exc:
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"[{exc.status_code}] {exc.mensagem}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("pedido %s: falha inesperada", pedido_id)
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"erro interno: {exc}")
        return
    await cliente_rl.entregar_resultado(pedido_id, status="concluido", resumo={}, resultado=status)


async def _processar_configurar_banco(pedido: dict) -> None:
    """RASCUNHO — mesma branch/aviso de _processar_status_agente. Só confirma
    que o pedido foi ACEITO pelo agent-vr (dispatched/queued); o desfecho real
    da conexão sai no db_status/db_error do próximo /status."""
    pedido_id = pedido["pedido_id"]
    agent_id = pedido.get("agent_id")
    banco = pedido.get("banco") or {}
    try:
        resultado = await agente.configurar_banco_agente(
            agent_id, banco.get("host", ""), banco.get("port", ""),
            banco.get("user", ""), banco.get("password", ""), banco.get("dbname", ""),
        )
    except agente.AgenteVRError as exc:
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"[{exc.status_code}] {exc.mensagem}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("pedido %s: falha inesperada", pedido_id)
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"erro interno: {exc}")
        return
    await cliente_rl.entregar_resultado(pedido_id, status="concluido", resumo={}, resultado=resultado)


async def _processar_corrigir(pedido: dict) -> None:
    """RASCUNHO (Fase 3, branch draft/escrita-ncm-auto do agent-vr — ainda não
    revisada com o time). Repassa a correção pro agent-vr e devolve o
    desfecho JÁ RESOLVIDO (a chamada de lá espera dry-run + execução antes de
    responder) — não tem um segundo polling aqui, ao contrário de
    configurar_banco."""
    pedido_id = pedido["pedido_id"]
    agent_id = pedido.get("agent_id")
    correcao = pedido.get("correcao") or {}
    task_id = correcao.get("task_id") or ""
    params = correcao.get("params") or {}
    try:
        resultado = await agente.corrigir_agente(agent_id, task_id, params)
    except agente.AgenteVRError as exc:
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"[{exc.status_code}] {exc.mensagem}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("pedido %s: falha inesperada", pedido_id)
        await cliente_rl.entregar_resultado(pedido_id, status="erro", erro=f"erro interno: {exc}")
        return
    await cliente_rl.entregar_resultado(pedido_id, status="concluido", resumo={}, resultado=resultado)


async def _processar(pedido: dict) -> None:
    tipo = pedido.get("tipo") or "sincronizar"
    if tipo == "provisionar":
        await _processar_provisionamento(pedido)
    elif tipo == "status_agente":
        await _processar_status_agente(pedido)
    elif tipo == "configurar_banco":
        await _processar_configurar_banco(pedido)
    elif tipo == "corrigir":
        await _processar_corrigir(pedido)
    else:
        await _processar_sincronizacao(pedido)


async def _ciclo_heartbeat() -> None:
    while True:
        try:
            diag = await diagnostico()
            await cliente_rl.enviar_heartbeat(diag)
            log.info(
                "heartbeat: escopo=%d/%d tarefa(s), base RFB=%s",
                len(diag["escopo"]), len(diag["escopo"]) + len(diag["tarefas_faltando_no_escopo"]),
                diag["base_rfb_disponivel"],
            )
        except Exception:  # noqa: BLE001 — heartbeat nunca deve derrubar o processo
            log.exception("falha ao enviar heartbeat")
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def _ciclo_pedidos() -> None:
    while True:
        try:
            pedido = await cliente_rl.proximo_pedido()
        except Exception:  # noqa: BLE001
            log.exception("falha no long-poll do RL — tentando de novo em %ss", RETRY_BACKOFF_S)
            await asyncio.sleep(RETRY_BACKOFF_S)
            continue

        if pedido is None:
            continue  # long-poll só voltou vazio; chama de novo na hora

        try:
            await _processar(pedido)
        except Exception:  # noqa: BLE001 — nunca deixa um pedido ruim matar o loop
            log.exception("pedido %s: falha ao entregar resultado", pedido.get("pedido_id"))
            await asyncio.sleep(RETRY_BACKOFF_S)


def _checar_config() -> None:
    faltando = [
        nome for nome in ("PONTE_API_KEY",) if not os.getenv(nome)
    ]
    if not agente.integracao_configurada():
        faltando.append("AGENTE_VR_API_KEY")
    if faltando:
        log.error("configuração incompleta, faltando: %s — ver .env.example", ", ".join(faltando))
        sys.exit(1)
    if not agente.provisionamento_configurado():
        # não é fatal: sincronização continua funcionando sem isso, só o
        # /provisionar do RL fica indisponível (a ponte reporta o erro por
        # pedido, não recusa subir).
        log.warning("AGENTE_VR_PROVISIONAMENTO_API_KEY ausente — pedidos de "
                    "provisionamento vão falhar até configurar")


async def main() -> None:
    _checar_config()
    log.info("ponte do agente vr iniciando — RL=%s, cérebro=%s", cliente_rl.RL_BASE_URL, agente.API_URL)
    log.info("hora local: %s", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    await asyncio.gather(_ciclo_pedidos(), _ciclo_heartbeat())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("encerrado.")
