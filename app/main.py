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


async def _processar(pedido: dict) -> None:
    pedido_id = pedido["pedido_id"]
    client_id = pedido["client_id"]
    agent_id = pedido.get("agent_id")
    cclasstrib_lista = pedido.get("cclasstrib_lista") or []

    log.info("processando pedido %s (client_id=%s)", pedido_id, client_id)
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
