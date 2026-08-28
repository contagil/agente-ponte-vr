"""
Orquestração da sincronização — dispara as 28 consultas no cérebro do
agent-vr e roda as análises tributárias sobre o resultado.

Isto morava no backend do Reforma Legal até 04/08/2026; migrou pra cá porque
o RL é externo e nunca deveria ter tido a AGENTE_VR_API_KEY nem o pacote da
Calculadora RFB — ver README.md deste projeto.
"""
import asyncio
import os
from datetime import date, datetime, timezone

from app.core import agente_vr_client as agente
from app.core import analise_vr, rfb_reference
from app import cliente_rl

# Consultas do catálogo do cérebro, agrupadas por análise. O task_id vem de
# env var (`AGENTE_VR_TASK_<CHAVE>`) porque o nome é acordado com quem cadastra
# a tarefa no console do agent-vr — se lá usarem outro id, muda sem redeploy.
#
# Grupos marcados como `obrigatorio` derrubam a sincronização se falharem (são
# o núcleo tributário); os demais viram "análise indisponível" e o resto segue.
# É de propósito: `reformatributaria.evento`, `pdv.parametro` e
# `public.escritaitem` não existem em toda instalação do VR.
GRUPOS: dict[str, dict] = {
    "ncm": {
        "obrigatorio": True,
        "chaves": ["ncm_cadastrado", "ncm_produtos"],
    },
    "cst": {
        "obrigatorio": True,
        "chaves": ["cst"],
    },
    "cclasstrib": {
        "obrigatorio": True,
        "chaves": ["cclasstrib", "cclasstrib_ncm", "cclasstrib_produto", "ncm_ativos"],
    },
    "uf_municipio_cbs": {
        "obrigatorio": False,
        "chaves": ["estados", "ibs_estadual", "ibs_municipal", "municipios", "cbs"],
    },
    "vinculo": {
        "obrigatorio": False,
        "chaves": ["vinculo_produtos", "vinculo_produto", "vinculo_ncm", "classificacoes"],
    },
    "debito_credito": {
        "obrigatorio": False,
        "chaves": ["tipodebitocredito", "debitocredito", "tiposaida", "cfoptiposaida"],
    },
    "tiposaida": {
        "obrigatorio": False,
        # o cruzamento com a escrituração fiscal é opcional dentro do grupo:
        # sem ele a regra de cadastro continua valendo (ver analise_tiposaida)
        "chaves": ["movimento_tiposaida", "operacoes_sem_tiposaida"],
    },
    "parametro_data": {
        "obrigatorio": False,
        "chaves": ["parametro_nfe", "parametro_pdv"],
    },
    "eventos": {
        "obrigatorio": False,
        "chaves": ["tipoautor", "tipoevento", "eventos_totais"],
    },
    "empresas": {
        "obrigatorio": False,
        "chaves": ["empresas"],
    },
}

# Onde cada grupo indisponível é sinalizado no resultado. O grupo `tiposaida`
# não aparece aqui de propósito: sem o movimento a análise 11 continua saindo,
# só sem a informação de "chegou a ser usado" (`movimento_disponivel: false`).
_CHAVE_RESULTADO = {
    "uf_municipio_cbs": "analise_4_5_6_uf_municipio_cbs",
    "vinculo": "analise_7_vinculo",
    "debito_credito": "analise_8_debito_credito",
    "parametro_data": "analise_9_parametro_data_ibscbs",
    "eventos": "analise_10_eventos",
}

_TASK_IDS_PADRAO = {
    "ncm_cadastrado": "reforma_ncm_cadastrado",
    "ncm_produtos": "reforma_ncm_produtos",
    "ncm_ativos": "reforma_ncm_ativos",
    "cst": "reforma_cst_cadastrado",
    "cclasstrib": "reforma_cclasstrib_cadastrada",
    "cclasstrib_ncm": "reforma_cclasstrib_vinculo_ncm",
    "cclasstrib_produto": "reforma_cclasstrib_vinculo_produto",
    "estados": "reforma_estados",
    "ibs_estadual": "reforma_ibs_estadual",
    "ibs_municipal": "reforma_ibs_municipal",
    "municipios": "reforma_municipios",
    "cbs": "reforma_cbs",
    "vinculo_produtos": "reforma_vinculo_produtos",
    "vinculo_produto": "reforma_vinculo_produto",
    "vinculo_ncm": "reforma_vinculo_ncm",
    "classificacoes": "reforma_classificacoes",
    "tipodebitocredito": "reforma_tipodebitocredito",
    "debitocredito": "reforma_debitocredito",
    "tiposaida": "reforma_tiposaida",
    "cfoptiposaida": "reforma_cfoptiposaida",
    "movimento_tiposaida": "reforma_movimento_tiposaida",
    "operacoes_sem_tiposaida": "reforma_operacoes_sem_tiposaida",
    "parametro_nfe": "reforma_parametro_nfe",
    "parametro_pdv": "reforma_parametro_pdv",
    "tipoautor": "reforma_tipoautor",
    "tipoevento": "reforma_tipoevento",
    "eventos_totais": "reforma_eventos_totais",
    "empresas": "reforma_empresas",
}

TAREFAS = {
    chave: os.getenv(f"AGENTE_VR_TASK_{chave.upper()}", padrao)
    for chave, padrao in _TASK_IDS_PADRAO.items()
}

TAREFAS_OBRIGATORIAS = [
    TAREFAS[chave]
    for grupo in GRUPOS.values() if grupo["obrigatorio"]
    for chave in grupo["chaves"]
]


class SincronizacaoError(Exception):
    """Falha num grupo obrigatório — a sincronização inteira para."""


async def sincronizar_cliente(client_id: str, agent_id: str | None,
                              cclasstrib_lista: list[str]) -> dict:
    """Dispara as 28 consultas (sequencial — o cérebro aceita no máximo 2 por
    agente em voo, §7 da proposta) e roda as análises. Devolve o `resultado`
    completo, no mesmo formato que a tela do RL sempre consumiu."""
    if not rfb_reference.base_disponivel():
        raise SincronizacaoError(
            "Base oficial da Receita Federal indisponível nesta ponte "
            "(configure RFB_CALCULADORA_DIR)."
        )

    coletas: dict[str, list[dict]] = {}
    indisponiveis: dict[str, str] = {}
    origem_resposta = None

    for nome_grupo, grupo in GRUPOS.items():
        try:
            for chave in grupo["chaves"]:
                resposta = await agente.consultar(TAREFAS[chave], client_id, agent_id=agent_id)
                coletas[chave] = resposta.como_dicts()
                if origem_resposta is None:
                    origem_resposta = resposta
        except agente.AgenteVRError as exc:
            if grupo["obrigatorio"]:
                raise SincronizacaoError(f"[{exc.status_code}] {exc.mensagem}") from exc
            # tabela que não existe nesta instalação do VR, ou tarefa fora do
            # escopo: a análise correspondente sai do relatório, o resto segue
            indisponiveis[nome_grupo] = exc.mensagem
            for chave in grupo["chaves"]:
                coletas.pop(chave, None)

    def rodar_analises() -> dict:
        """Roda fora do event loop: monta/lê o SQLite da RFB e o JSON de NCM
        (~3MB), ambos bloqueantes."""
        rfb_conn = rfb_reference.conectar_base()
        try:
            analise_3 = analise_vr.analise_cclasstrib(
                coletas["cclasstrib"], coletas["cclasstrib_ncm"],
                coletas["cclasstrib_produto"], coletas["ncm_ativos"], rfb_conn,
            )
            cadastrados_no_vr = {c["cclasstrib"] for c in analise_3["cclasstrib_cadastrados"]}
            lista_manual = analise_vr.validar_lista_manual(
                rfb_conn, cclasstrib_lista, cadastrados_no_vr
            )

            resultado = {
                "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "referencia": date.today().isoformat(),
                "analise_1_ncm": analise_vr.analise_ncm(
                    coletas["ncm_cadastrado"], coletas["ncm_produtos"]
                ),
                "analise_2_cst": analise_vr.analise_cst(coletas["cst"], rfb_conn),
                "analise_3_cclasstrib": analise_3,
                "analise_3b_lista_manual": [
                    v for v in lista_manual if not v.get("cadastrado_no_vr")
                ],
            }

            if "uf_municipio_cbs" not in indisponiveis:
                resultado["analise_4_5_6_uf_municipio_cbs"] = analise_vr.analise_uf_municipio_cbs(
                    coletas["estados"], coletas["ibs_estadual"], coletas["ibs_municipal"],
                    coletas["municipios"], coletas["cbs"], rfb_conn,
                )

            if "vinculo" not in indisponiveis:
                eans = [p.get("ean") for p in coletas["vinculo_produtos"]]
                try:
                    ean_lookup = await cliente_rl.lookup_ean(eans)
                except Exception:
                    # cruzamento por EAN é um refinamento, não pré-requisito — se o
                    # RL estiver fora do ar, a análise segue só com NCM (comportamento
                    # de antes desta mudança), não derruba a sincronização inteira
                    ean_lookup = {}
                analise_7 = analise_vr.analise_vinculo(
                    coletas["vinculo_produtos"], coletas["vinculo_produto"],
                    coletas["vinculo_ncm"], coletas["classificacoes"], rfb_conn,
                    ean_lookup=ean_lookup,
                )
                analise_vr.aplicar_contagem_efetiva_cclasstrib(analise_3, analise_7)
                resultado["analise_7_vinculo"] = analise_vr.enxugar_vinculo(analise_7)

            if "debito_credito" not in indisponiveis:
                resultado["analise_8_debito_credito"] = analise_vr.analise_debito_credito(
                    coletas["tipodebitocredito"], coletas["debitocredito"],
                    coletas["tiposaida"], coletas["cfoptiposaida"],
                )
                resultado["analise_11_tiposaida"] = analise_vr.analise_tiposaida(
                    coletas["tiposaida"], coletas["cfoptiposaida"],
                    coletas["debitocredito"], coletas["cclasstrib"],
                    movimento_linhas=coletas.get("movimento_tiposaida"),
                    sem_cadastro_linhas=coletas.get("operacoes_sem_tiposaida"),
                    rfb_conn=rfb_conn,
                )

            if "parametro_data" not in indisponiveis and "uf_municipio_cbs" not in indisponiveis:
                resultado["analise_9_parametro_data_ibscbs"] = analise_vr.analise_parametro_data_ibscbs(
                    coletas["parametro_nfe"], coletas["parametro_pdv"], coletas["cbs"],
                )

            if "eventos" not in indisponiveis:
                resultado["analise_10_eventos"] = analise_vr.analise_eventos(
                    coletas["tipoautor"], coletas["tipoevento"], coletas.get("eventos_totais"),
                )

            if "empresas" not in indisponiveis:
                resultado["empresas"] = analise_vr.listar_empresas(coletas["empresas"])

            for nome_grupo, motivo in indisponiveis.items():
                chave_resultado = _CHAVE_RESULTADO.get(nome_grupo)
                if chave_resultado:
                    resultado[chave_resultado] = {"indisponivel": True, "motivo": motivo}
            if "debito_credito" in indisponiveis:
                resultado["analise_11_tiposaida"] = {
                    "indisponivel": True, "motivo": indisponiveis["debito_credito"],
                }
            resultado["analises_indisponiveis"] = indisponiveis

            resultado["origem"] = {
                "client_id": getattr(origem_resposta, "client_id", None) or client_id,
                "agent_id": getattr(origem_resposta, "agent_id", None),
                "executado_em": getattr(origem_resposta, "executado_em", None),
            }
            return resultado
        finally:
            rfb_conn.close()

    try:
        return await asyncio.to_thread(rodar_analises)
    except (rfb_reference.BaseRFBIndisponivel, FileNotFoundError) as exc:
        raise SincronizacaoError(str(exc)) from exc


async def diagnostico() -> dict:
    """Para o heartbeat: escopo concedido e o que falta, sem disparar nada."""
    escopo: list[str] = []
    try:
        info = await agente.whoami()
        escopo = info.get("scopes", [])
    except agente.AgenteVRError:
        pass
    return {
        "escopo": escopo,
        "base_rfb_disponivel": rfb_reference.base_disponivel(),
        "tarefas_obrigatorias_faltando": sorted(t for t in TAREFAS_OBRIGATORIAS if t not in escopo),
        "tarefas_faltando_no_escopo": sorted(t for t in TAREFAS.values() if t not in escopo),
    }
