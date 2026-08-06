"""
Valida as consultas do Agente VR contra um banco do VR real, ANTES de cadastrá-las
no catálogo do console.

Por que existe: o SQL das tarefas vive no catálogo do agent-vr, cadastrado à mão.
Se uma coluna não existir naquela instalação, o erro só aparece quando o usuário
clica em sincronizar. Este script roda as mesmas consultas direto no banco e diz
quais funcionam, quais colunas cada uma devolve e se batem com o que o backend
espera ler.

O SQL é lido de `docs/agente-vr-catalogo-tarefas.md` — o mesmo texto que vai ser
colado no console. Assim o teste prova o documento, não uma cópia dele.

SEGURANÇA
- A sessão é aberta em `readonly` e as consultas passam pelo mesmo filtro do
  catálogo (só SELECT/WITH, um statement). Nada aqui escreve no banco.
- A senha vem de `PGPASSWORD` (ou do prompt) — nunca de arquivo ou argumento.
- Rode SOMENTE contra o banco de teste (`vr_teste`), como manda a regra de ouro
  do agent-vr.

USO
    set PGPASSWORD=...        (ou export, no bash)
    python scripts/validar_tarefas_agente_vr.py [--contar] [--analisar]

O default aponta para o banco de teste `vr_teste` em 10.0.15.5:8745. Rodando de
dentro da própria VM, `--host localhost` também serve.

    --contar    também conta as linhas de cada consulta (mais lento; é o número
                que orienta o `max_rows` no cadastro da tarefa)
    --analisar  roda as análises do Reforma Legal sobre os dados reais e grava
                o relatório em JSON (precisa da base oficial da RFB configurada)
"""
import argparse
import getpass
import json
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# os símbolos ✓/✗ e os acentos estouram no cp1252 quando a saída é redirecionada
# (pipe, arquivo) em vez de ir para o console
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import psycopg2  # noqa: E402

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "agente-vr-catalogo-tarefas.md")

# Colunas que o backend lê de cada consulta (app/core/analise_vr.py). Faltando
# uma delas, a análise não quebra: ela lê vazio e o resultado sai errado em
# silêncio — daí a conferência explícita aqui.
COLUNAS_ESPERADAS = {
    "reforma_ncm_cadastrado": ["id", "ncm1", "ncm2", "ncm3", "descricao", "id_situacaocadastro"],
    "reforma_ncm_produtos": ["id_produto", "ncm", "ean", "descricao"],
    "reforma_ncm_ativos": ["ncm1", "ncm2", "ncm3"],
    "reforma_cst_cadastrado": ["cst", "descricao", "id_situacaocadastro", "grupoibscbs",
                               "grupoibscbsmono", "gruporeducao", "grupodiferimento"],
    "reforma_cclasstrib_cadastrada": ["id", "cclasstrib", "descricao", "cst", "aliquotazero",
                                      "id_situacaocadastro", "grupoibscbs"],
    "reforma_cclasstrib_vinculo_ncm": ["cclasstrib", "qtd"],
    "reforma_cclasstrib_vinculo_produto": ["cclasstrib", "qtd"],
    "reforma_estados": ["id", "sigla", "descricao"],
    "reforma_ibs_estadual": ["id", "id_estado", "porcentagem", "datainicio", "datatermino"],
    "reforma_ibs_municipal": ["id", "id_municipio", "municipio", "sigla", "porcentagem",
                              "datainicio", "datatermino"],
    "reforma_municipios": ["sigla", "municipio", "cadastrado"],
    "reforma_cbs": ["id", "porcentagem", "datainicio", "datatermino", "id_situacaocadastro"],
    "reforma_vinculo_produtos": ["id_produto", "ncm1", "ncm2", "ncm3", "ean", "id_loja", "descricao"],
    "reforma_vinculo_produto": ["id_produto", "id_loja", "id_classificacao"],
    "reforma_vinculo_ncm": ["ncm1", "ncm2", "ncm3", "id_loja", "id_classificacao"],
    "reforma_classificacoes": ["id", "cclasstrib", "descricao", "cst"],
    "reforma_tipodebitocredito": ["id"],
    "reforma_debitocredito": ["id", "cod_xml", "descricao", "id_situacaocadastro", "sigla",
                              "tipo_descricao"],
    "reforma_tiposaida": ["id", "descricao", "id_situacaocadastro", "id_debitocredito",
                          "id_classificacaotributaria"],
    "reforma_cfoptiposaida": ["id_tiposaida", "cfop", "descricao_cfop", "linha"],
    "reforma_movimento_tiposaida": ["id_tiposaida", "cfop", "qtd"],
    "reforma_operacoes_sem_tiposaida": ["cfop", "qtd"],
    "reforma_parametro_nfe": ["id_loja", "loja", "valor", "descricao_parametro"],
    "reforma_parametro_pdv": ["id_loja", "loja", "valor", "descricao_parametro"],
    "reforma_tipoautor": ["id", "codigo", "descricao"],
    "reforma_tipoevento": ["codigo", "descricao", "id_tipoautor"],
    "reforma_eventos_totais": ["qtd_evento", "qtd_eventoitem"],
    "reforma_empresas": ["loja", "razaosocial", "cnpj"],
}

# Consultas cuja ausência é esperada em parte das instalações — falha aqui é
# aviso, não erro (o backend já trata como "análise indisponível").
OPCIONAIS = {
    "reforma_movimento_tiposaida", "reforma_operacoes_sem_tiposaida",
    "reforma_parametro_nfe", "reforma_parametro_pdv",
    "reforma_tipoautor", "reforma_tipoevento", "reforma_eventos_totais",
}

VERDE, VERMELHO, AMARELO, CINZA, FIM = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"


def extrair_sql_do_doc(caminho=DOC):
    """Lê os pares (task_id, SQL) dos blocos ```sql do documento do catálogo."""
    with open(caminho, encoding="utf-8") as f:
        texto = f.read()
    tarefas = {}
    padrao = re.compile(
        r"^#{2,3}\s+`(reforma_[a-z0-9_]+)`\s*$.*?```sql\n(.*?)\n```",
        re.MULTILINE | re.DOTALL,
    )
    for m in padrao.finditer(texto):
        tarefas[m.group(1)] = m.group(2).strip()
    return tarefas


def politica_do_catalogo(sql):
    """Mesma checagem que o console e o agente aplicam (catalog.py/job.go)."""
    corpo = sql.strip()
    cabeca = corpo.upper()
    if not (cabeca.startswith("SELECT") or cabeca.startswith("WITH")):
        return "não começa com SELECT/WITH"
    if ";" in corpo.rstrip("; \n\t"):
        return "mais de um statement"
    if re.findall(r"\$\d+", corpo):
        return "usa placeholder $n (as tarefas do RL não têm parâmetro)"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="10.0.15.5")
    ap.add_argument("--port", default="8745")
    ap.add_argument("--dbname", default="vr_teste")
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--contar", action="store_true", help="contar linhas de cada consulta")
    ap.add_argument("--analisar", action="store_true", help="rodar as análises sobre os dados reais")
    ap.add_argument("--timeout", type=int, default=120, help="statement_timeout por consulta (s)")
    ap.add_argument("--saida", default="relatorio_agente_vr.json")
    args = ap.parse_args()

    if args.dbname != "vr_teste":
        print(f"{AMARELO}Atenção: o alvo não é 'vr_teste'. A regra de ouro do agent-vr manda "
              f"testar só no banco de teste.{FIM}")
        if input(f"Continuar mesmo assim em '{args.dbname}'? (digite SIM) ").strip() != "SIM":
            return 1

    senha = os.getenv("PGPASSWORD") or getpass.getpass("Senha do Postgres: ")

    tarefas = extrair_sql_do_doc()
    print(f"{len(tarefas)} consultas lidas de docs/agente-vr-catalogo-tarefas.md\n")

    conn = psycopg2.connect(host=args.host, port=args.port, dbname=args.dbname,
                            user=args.user, password=senha, connect_timeout=10)
    # nenhuma escrita é possível a partir daqui, mesmo por engano
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT version();")
    print(f"{CINZA}{cur.fetchone()[0]}{FIM}\n")
    cur.execute(f"SET statement_timeout = {args.timeout * 1000};")

    dados, falhas, avisos = {}, [], []

    for task_id in sorted(tarefas):
        sql = tarefas[task_id]
        opcional = task_id in OPCIONAIS
        rotulo = f"{task_id:38}"

        problema = politica_do_catalogo(sql)
        if problema:
            print(f"{VERMELHO}✗{FIM} {rotulo} política do catálogo: {problema}")
            falhas.append(task_id)
            continue

        corpo = sql.rstrip("; \n\t")
        try:
            # EXPLAIN valida tabelas/colunas sem executar nada
            cur.execute(f"EXPLAIN {corpo}")
            cur.execute(f"SELECT * FROM ({corpo}) AS t LIMIT 5")
            colunas = [d[0] for d in cur.description]
            amostra = cur.fetchall()
        except psycopg2.Error as exc:
            msg = str(exc).splitlines()[0]
            cor = AMARELO if opcional else VERMELHO
            marca = "!" if opcional else "✗"
            print(f"{cor}{marca}{FIM} {rotulo} {msg}")
            (avisos if opcional else falhas).append(task_id)
            continue

        faltando = [c for c in COLUNAS_ESPERADAS.get(task_id, []) if c not in colunas]
        qtd = ""
        if args.contar:
            try:
                cur.execute(f"SELECT count(*) FROM ({corpo}) AS t")
                total = cur.fetchone()[0]
                qtd = f" · {total:,} linhas".replace(",", ".")
                if total > 500_000:
                    qtd += f" {VERMELHO}(acima do max_rows máximo!){FIM}"
                    avisos.append(task_id)
            except psycopg2.Error as exc:
                qtd = f" · {AMARELO}contagem falhou/timeout{FIM}"
                conn.rollback() if not conn.autocommit else None

        if faltando:
            print(f"{VERMELHO}✗{FIM} {rotulo} colunas faltando: {', '.join(faltando)}")
            print(f"  {CINZA}devolveu: {', '.join(colunas)}{FIM}")
            falhas.append(task_id)
        else:
            print(f"{VERDE}✓{FIM} {rotulo} {len(colunas)} colunas{qtd}")

        dados[task_id] = {"colunas": colunas, "amostra": [list(map(str, l)) for l in amostra]}

    cur.close()

    print()
    if falhas:
        print(f"{VERMELHO}{len(falhas)} consulta(s) com problema: {', '.join(sorted(set(falhas)))}{FIM}")
        print("Ajuste o SQL em docs/agente-vr-catalogo-tarefas.md antes de cadastrar no console.")
    else:
        print(f"{VERDE}Todas as consultas obrigatórias passaram.{FIM}")
    if avisos:
        print(f"{AMARELO}Avisos ({len(set(avisos))}): {', '.join(sorted(set(avisos)))}{FIM}")
        print("Consultas opcionais que falharam viram 'análise indisponível' na tela — sem quebrar o resto.")

    if args.analisar and not falhas:
        rodar_analises(conn, args)

    conn.close()
    return 1 if falhas else 0


def rodar_analises(conn, args):
    """Etapa 2: alimenta as análises do Reforma Legal com os dados reais.

    Vai direto ao banco, sem agente nem cérebro — o formato entregue é o mesmo
    (lista de dicts por nome de coluna), então exercita exatamente o código que
    roda em produção.
    """
    from app.core import analise_vr, rfb_reference

    print("\n--- análises sobre os dados reais ---")
    if not rfb_reference.base_disponivel():
        print(f"{AMARELO}Base oficial da RFB indisponível (RFB_CALCULADORA_DIR); "
              f"só a análise 1 (NCM) será gerada.{FIM}")

    tarefas = extrair_sql_do_doc()
    cur = conn.cursor()

    def linhas(task_id):
        cur.execute(tarefas[task_id].rstrip("; \n\t"))
        colunas = [d[0].strip().lower() for d in cur.description]
        return [dict(zip(colunas, map(_texto, linha))) for linha in cur.fetchall()]

    resultado = {"gerado_em": datetime.now().isoformat(timespec="seconds"),
                 "referencia": date.today().isoformat()}
    resultado["analise_1_ncm"] = analise_vr.analise_ncm(
        linhas("reforma_ncm_cadastrado"), linhas("reforma_ncm_produtos"))
    print(f"análise 1: {resultado['analise_1_ncm']['total_ativos_invalidos']} NCM inválido(s) ativo(s)")

    if rfb_reference.base_disponivel():
        rfb = rfb_reference.conectar_base()
        try:
            resultado["analise_2_cst"] = analise_vr.analise_cst(linhas("reforma_cst_cadastrado"), rfb)
            a3 = analise_vr.analise_cclasstrib(
                linhas("reforma_cclasstrib_cadastrada"),
                linhas("reforma_cclasstrib_vinculo_ncm"),
                linhas("reforma_cclasstrib_vinculo_produto"),
                linhas("reforma_ncm_ativos"), rfb)
            resultado["analise_3_cclasstrib"] = a3
            resultado["analise_4_5_6_uf_municipio_cbs"] = analise_vr.analise_uf_municipio_cbs(
                linhas("reforma_estados"), linhas("reforma_ibs_estadual"),
                linhas("reforma_ibs_municipal"), linhas("reforma_municipios"),
                linhas("reforma_cbs"), rfb)
            a7 = analise_vr.analise_vinculo(
                linhas("reforma_vinculo_produtos"), linhas("reforma_vinculo_produto"),
                linhas("reforma_vinculo_ncm"), linhas("reforma_classificacoes"), rfb)
            analise_vr.aplicar_contagem_efetiva_cclasstrib(a3, a7)
            resultado["analise_7_vinculo"] = analise_vr.enxugar_vinculo(a7)
            resultado["analise_8_debito_credito"] = analise_vr.analise_debito_credito(
                linhas("reforma_tipodebitocredito"), linhas("reforma_debitocredito"),
                linhas("reforma_tiposaida"), linhas("reforma_cfoptiposaida"))
            resultado["analise_11_tiposaida"] = analise_vr.analise_tiposaida(
                linhas("reforma_tiposaida"), linhas("reforma_cfoptiposaida"),
                linhas("reforma_debitocredito"), linhas("reforma_cclasstrib_cadastrada"),
                movimento_linhas=_opcional(linhas, "reforma_movimento_tiposaida", conn),
                sem_cadastro_linhas=_opcional(linhas, "reforma_operacoes_sem_tiposaida", conn))
            nfe = _opcional(linhas, "reforma_parametro_nfe", conn)
            pdv = _opcional(linhas, "reforma_parametro_pdv", conn)
            if nfe is not None and pdv is not None:
                resultado["analise_9_parametro_data_ibscbs"] = analise_vr.analise_parametro_data_ibscbs(
                    nfe, pdv, linhas("reforma_cbs"))
            autor = _opcional(linhas, "reforma_tipoautor", conn)
            evento = _opcional(linhas, "reforma_tipoevento", conn)
            if autor is not None and evento is not None:
                resultado["analise_10_eventos"] = analise_vr.analise_eventos(
                    autor, evento, _opcional(linhas, "reforma_eventos_totais", conn))
            resultado["empresas"] = analise_vr.listar_empresas(linhas("reforma_empresas"))
        finally:
            rfb.close()

    resumo = analise_vr.resumir(resultado)
    print("\nresumo:", json.dumps(resumo, ensure_ascii=False, indent=2))

    with open(args.saida, "w", encoding="utf-8") as f:
        json.dump({"resumo": resumo, "resultado": resultado}, f, ensure_ascii=False, indent=2)
    tamanho = os.path.getsize(args.saida) / 1024
    print(f"\nrelatório completo em {args.saida} ({tamanho:.0f} KB)")
    cur.close()


def _texto(valor):
    """O agente entrega tudo como string (o resultado passa por CSV); aqui é o
    mesmo tratamento, para exercitar o código no formato real."""
    if valor is None:
        return ""
    if isinstance(valor, (dict, list)):
        return json.dumps(valor)
    if isinstance(valor, bool):
        return "t" if valor else "f"
    return str(valor)


def _opcional(linhas, task_id, conn):
    try:
        return linhas(task_id)
    except psycopg2.Error as exc:
        print(f"{AMARELO}  {task_id}: indisponível ({str(exc).splitlines()[0]}){FIM}")
        return None


if __name__ == "__main__":
    sys.exit(main())
