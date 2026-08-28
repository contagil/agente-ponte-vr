"""
Análises da configuração da Reforma Tributária (IBS/CBS) no banco do VR Software.

Portado de `analise_reforma.py` (projeto `configuracao-vr-leitura-reforma-tributaria`),
com uma diferença estrutural: lá o SQL e o veredito viviam juntos, rodando sobre um
cursor psycopg2 aberto direto no banco do cliente. Aqui o SQL vive no catálogo do
cérebro do Agente VR (o agente é quem alcança o banco), e este módulo recebe só as
linhas já capturadas — a lógica de cruzamento com a base oficial da RFB é a mesma.

Consequência prática: **todo valor chega como string** (o cérebro relê o resultado
de um CSV; NULL vira string vazia). Daí os coeruso de tipo em `_int`/`_bool`/`_txt`.

Escopo desta primeira etapa: análises 1 (NCM), 2 (CST) e 3 (CClassTrib) + a
conferência da lista manual de supermercado.
"""
import json
import os
import re
import unicodedata
from datetime import date, datetime
from functools import lru_cache

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NCM_VIGENTE_JSON = os.getenv(
    "NCM_VIGENTE_JSON",
    os.path.join(_BASE_DIR, "data", "Tabela_NCM_Vigente_20260723.json"),
)

# CClassTrib que um supermercado tipicamente precisa ter cadastradas — a mesma
# lista usada no app desktop de auditoria.
LISTA_CCLASSTRIB_SUPERMERCADO = [
    "000001", "200003", "200013", "200014", "200034", "200035",
    "410008", "410001", "410002", "410014", "410030",
]

_STOPWORDS = {
    "de", "da", "do", "das", "dos", "e", "ou", "a", "o", "as", "os", "em",
    "no", "na", "nos", "nas", "com", "sem", "para", "por", "que", "ao",
    "aos", "sua", "seu", "suas", "seus", "quando", "se",
}


# ---------------------------------------------------------------------------
# Coerção dos valores que chegam do agente (tudo string)
# ---------------------------------------------------------------------------

def _txt(valor) -> str:
    return (valor or "").strip()


def _int(valor, padrao: int = 0) -> int:
    try:
        return int(float(str(valor).strip()))
    except (TypeError, ValueError):
        return padrao


def _bool(valor) -> bool:
    """Booleano do Postgres serializado em CSV pode chegar como 't'/'true'/'1'."""
    return str(valor).strip().lower() in ("t", "true", "1", "s", "sim", "y", "yes")


def _data(valor):
    """Data do Postgres em CSV: 'YYYY-MM-DD' (às vezes com hora junto).
    Devolve None para vazio/inválido — quem chama trata ausência como 'em aberto'."""
    texto = _txt(valor)
    if not texto:
        return None
    texto = texto.replace("T", " ").split(" ")[0]
    try:
        return datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError:
        return None


def _float(valor, padrao=None):
    texto = _txt(valor).replace(",", ".")
    if not texto:
        return padrao
    try:
        return float(texto)
    except ValueError:
        return padrao


def _json(valor) -> dict:
    """Coluna `jsonb` serializada em CSV vira o texto do JSON.

    Usado nas tabelas cujo conjunto de colunas varia de instalação para
    instalação (`cfoptiposaida`): em vez de o SQL do catálogo citar uma coluna
    que pode não existir, a tarefa devolve a linha inteira como
    `to_jsonb(...)::text` e a decisão de qual campo existe é tomada aqui.

    O `::text` no SQL não é opcional: sem ele a coluna vai como `jsonb`, o
    driver do agente decodifica para um mapa Go e a formatação padrão produz
    `map[chave:valor]` — não é JSON. Isso já aconteceu (04/08/2026) e o
    resultado foi a análise reportando "sem vínculo" em tudo, em silêncio,
    porque o `except` engolia o erro. Por isso o aviso: se a tarefa no
    catálogo estiver com o SQL antigo, é aqui que isso vai aparecer de novo.
    """
    texto = _txt(valor)
    if not texto:
        return {}
    try:
        carga = json.loads(texto)
    except ValueError:
        if texto.startswith("map["):
            raise ValueError(
                "coluna JSON chegou como mapa Go, não JSON — a tarefa no catálogo "
                "do agent-vr precisa de `::text` depois do to_jsonb(...) "
                "(ver docs/agente-vr-catalogo-tarefas.md)"
            ) from None
        return {}
    return carga if isinstance(carga, dict) else {}


def _iso(valor):
    """Datas viram string na hora de gravar em JSONB / responder ao frontend."""
    return valor.isoformat() if isinstance(valor, date) else valor


def _normalizar_texto(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9 ]", " ", s).lower()
    return s


def _palavras_chave(s):
    return {p for p in _normalizar_texto(s).split() if len(p) > 2 and p not in _STOPWORDS}


def _mesma_raiz(palavra_a, palavra_b, prefixo_minimo=5):
    """Stemming bem simples: considera equivalentes palavras que compartilham
    um radical comum (ex.: 'tributado' / 'tributacao', 'integral' / 'integralmente')."""
    if palavra_a == palavra_b:
        return True
    menor_len = min(len(palavra_a), len(palavra_b))
    if menor_len < prefixo_minimo:
        return False
    tamanho_comparar = max(prefixo_minimo, menor_len - 3)
    return palavra_a[:tamanho_comparar] == palavra_b[:tamanho_comparar]


def _descricoes_equivalentes(desc_a, desc_b):
    """Compara duas descrições por palavras-chave/radical (não exige texto idêntico).
    Retorna (idêntica, equivalente, palavras_em_comum)."""
    norm_a, norm_b = _normalizar_texto(desc_a).strip(), _normalizar_texto(desc_b).strip()
    if norm_a == norm_b:
        return True, True, _palavras_chave(desc_a) & _palavras_chave(desc_b)
    palavras_a, palavras_b = _palavras_chave(desc_a), _palavras_chave(desc_b)
    comuns = palavras_a & palavras_b
    comuns_por_raiz = {pa for pa in palavras_a if any(_mesma_raiz(pa, pb) for pb in palavras_b)}
    todas_comuns = comuns | comuns_por_raiz
    menor = min(len(palavras_a), len(palavras_b)) or 1
    equivalente = len(todas_comuns) / menor >= 0.5
    return False, equivalente, todas_comuns


def _fmt_ncm(n1, n2, n3) -> str:
    return f"{_int(n1):04d}{_int(n2):02d}{_int(n3):02d}"


def _parse_data_br(s):
    d, m, a = s.split("/")
    if int(a) > 9000:
        return date(9999, 12, 31)
    return date(int(a), int(m), int(d))


# ---------------------------------------------------------------------------
# Análise 1 - NCM local x tabela NCM vigente (oficial)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=2)
def _carregar_ncm_oficial(caminho: str, mtime: float):
    """Cacheado por (caminho, mtime): o JSON tem ~3MB e não muda entre requisições,
    mas trocar o arquivo em disco invalida o cache sozinho."""
    with open(caminho, encoding="utf-8") as f:
        data = json.load(f)
    leaf_re = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})$")
    todos_leaf = {}
    for item in data["Nomenclaturas"]:
        m = leaf_re.match(item["Codigo"])
        if not m:
            continue
        codigo = m.group(1) + m.group(2) + m.group(3)
        todos_leaf[codigo] = (
            item["Descricao"],
            _parse_data_br(item["Data_Inicio"]),
            _parse_data_br(item["Data_Fim"]),
        )
    return todos_leaf


def _ncm_oficial(caminho: str | None = None):
    caminho = caminho or NCM_VIGENTE_JSON
    if not os.path.exists(caminho):
        raise FileNotFoundError(
            f"Tabela NCM vigente não encontrada em {caminho} "
            "(configure NCM_VIGENTE_JSON)."
        )
    return _carregar_ncm_oficial(caminho, os.path.getmtime(caminho))


# Um banco com a tabela de NCM desatualizada gera dezenas de milhares de
# linhas em `faltando_no_banco`/`produtos_ncm_invalido` — o resultado inteiro
# vai para uma coluna JSONB, então as listas longas ficam truncadas e o total
# é sempre reportado à parte.
LIMITE_LISTA = 500


def analise_ncm(ncm_cadastrados, produtos, ncm_vigente_json_path=None, hoje=None):
    """
    ncm_cadastrados: linhas de `public.ncm` nível 3 (id, ncm1, ncm2, ncm3,
        descricao, id_situacaocadastro, datainicio, datatermino).
    produtos: produtos ativos com NCM (id_produto, ncm, ean, descricao).
    """
    hoje = hoje or date.today()
    todos_leaf = _ncm_oficial(ncm_vigente_json_path)
    vigentes = {c: v[0] for c, v in todos_leaf.items() if v[1] <= hoje <= v[2]}

    db_ncm = {}
    for linha in ncm_cadastrados:
        codigo = _fmt_ncm(linha.get("ncm1"), linha.get("ncm2"), linha.get("ncm3"))
        db_ncm[codigo] = {
            "id": _int(linha.get("id")),
            "descricao": _txt(linha.get("descricao")),
            "ativo": _int(linha.get("id_situacaocadastro")) == 1,
        }

    faltando = sorted(set(vigentes) - set(db_ncm))
    invalidos_codigos = [
        codigo for codigo, info in db_ncm.items()
        if info["ativo"] and codigo not in vigentes
    ]

    produtos_por_ncm = {}
    for p in produtos:
        ncm = _txt(p.get("ncm"))
        produtos_por_ncm.setdefault(ncm, []).append({
            "id_produto": _int(p.get("id_produto")),
            "ean": _txt(p.get("ean")),
            "descricao": _txt(p.get("descricao")),
            "ncm": ncm,
        })

    invalidos = []
    for codigo in invalidos_codigos:
        info = db_ncm[codigo]
        motivo = (
            "não existe na tabela oficial"
            if codigo not in todos_leaf
            else f"encerrado em {todos_leaf[codigo][2]}"
        )
        invalidos.append({
            "ncm": codigo, "descricao": info["descricao"], "motivo": motivo,
            "qtd_produtos_vinculados": len(produtos_por_ncm.get(codigo, [])),
        })
    invalidos.sort(key=lambda x: x["qtd_produtos_vinculados"], reverse=True)

    produtos_ncm_invalido = [
        produto
        for item in invalidos
        for produto in produtos_por_ncm.get(item["ncm"], [])
    ]

    return {
        "total_vigentes_oficial": len(vigentes),
        "total_ncm_nivel3_banco": len(ncm_cadastrados),
        "total_faltando_no_banco": len(faltando),
        "total_ativos_invalidos": len(invalidos),
        "total_produtos_ncm_invalido": len(produtos_ncm_invalido),
        "faltando_no_banco": [
            {"ncm": c, "descricao": vigentes[c]} for c in faltando[:LIMITE_LISTA]
        ],
        "ativos_invalidos_no_banco": invalidos[:LIMITE_LISTA],
        "produtos_ncm_invalido": produtos_ncm_invalido[:LIMITE_LISTA],
    }


# ---------------------------------------------------------------------------
# Análise 2 - CST usados x base oficial
# ---------------------------------------------------------------------------

def _indicadores_esperados(descricao_vr):
    """Infere, a partir da descrição do CST, quais indicadores (manual VR seção
    6.1.1) deveriam estar marcados. Só devolve os que dá para deduzir com
    segurança pelo texto — `grupotribregular` fica de fora porque é usado em
    casos excepcionais não dedutíveis da descrição."""
    d = _normalizar_texto(descricao_vr)
    imune_suspenso = any(p in d for p in ("imunidade", "isenc", "suspens", "nao incid"))
    monofasico = "monof" in d
    reduzido = any(p in d for p in ("reduz", "reducao"))
    diferido = "diferi" in d

    if imune_suspenso:
        return {"grupoibscbs": False, "grupoibscbsmono": False,
                "gruporeducao": False, "grupodiferimento": False}

    return {
        "grupoibscbs": True,
        "grupoibscbsmono": monofasico,
        "gruporeducao": reduzido,
        "grupodiferimento": diferido,
    }


_LABEL_INDICADOR = {
    "grupoibscbs": "Grupo IBSCBS", "grupoibscbsmono": "Grupo IBSCBS Mono",
    "gruporeducao": "Grupo Redução", "grupodiferimento": "Grupo Diferimento",
}


def analise_cst(cst_cadastrados, rfb_conn, hoje=None):
    """cst_cadastrados: linhas de `reformatributaria.cst`."""
    hoje = (hoje or date.today()).isoformat()

    rcur = rfb_conn.cursor()
    rcur.execute(
        "SELECT SITR_CD, SITR_DESCRICAO FROM SITUACAO_TRIBUTARIA "
        "WHERE SITR_INICIO_VIGENCIA <= ? AND (SITR_FIM_VIGENCIA IS NULL OR SITR_FIM_VIGENCIA >= ?)",
        (hoje, hoje),
    )
    oficiais = {cd: desc for cd, desc in rcur.fetchall()}
    rcur.close()

    resultado = []
    for linha in cst_cadastrados:
        cst = _txt(linha.get("cst"))
        desc_vr = _txt(linha.get("descricao"))
        desc_of = oficiais.get(cst)

        # os indicadores do CST (manual 6.1.1) têm prioridade sobre a checagem
        # de descrição: indicador errado muda o cálculo, descrição divergente não
        atual = {
            "grupoibscbs": _bool(linha.get("grupoibscbs")),
            "grupoibscbsmono": _bool(linha.get("grupoibscbsmono")),
            "gruporeducao": _bool(linha.get("gruporeducao")),
            "grupodiferimento": _bool(linha.get("grupodiferimento")),
        }
        esperado = _indicadores_esperados(desc_vr)
        divergentes = [k for k, v in esperado.items() if atual.get(k) != v]

        if divergentes:
            detalhes = "; ".join(
                f"{_LABEL_INDICADOR[k]} deveria ser {'MARCADO' if esperado[k] else 'DESMARCADO'}"
                for k in divergentes
            )
            status = f"ERRO: indicador incorreto - {detalhes}"
        elif desc_of is None:
            status = "CST NÃO ENCONTRADO NA BASE OFICIAL VIGENTE"
        else:
            identica, equivalente, _comuns = _descricoes_equivalentes(desc_vr, desc_of)
            if identica:
                status = "OK (código e descrição idênticos)"
            elif equivalente:
                status = "OK (código correto - descrição equivalente por palavra-chave)"
            else:
                status = "CÓDIGO OK - DESCRIÇÃO DIVERGENTE (conferir manualmente)"

        resultado.append({
            "cst": cst, "descricao_vr": desc_vr, "descricao_oficial": desc_of,
            "status": status, "ativo": _int(linha.get("id_situacaocadastro")) == 1,
        })

    cadastrados = {_txt(c.get("cst")) for c in cst_cadastrados}
    nao_usados = sorted(set(oficiais) - cadastrados)

    return {
        "cst_cadastrados": resultado,
        "cst_vigentes_nao_cadastrados": [
            {"cst": c, "descricao": oficiais[c]} for c in nao_usados
        ],
    }


# ---------------------------------------------------------------------------
# Análise 3 - CClassTrib: cadastradas x oficial x sortimento ativo
# ---------------------------------------------------------------------------

def analise_cclasstrib(cclasstrib_cadastradas, vinculo_ncm, vinculo_produto,
                       ncms_ativos_linhas, rfb_conn, hoje=None):
    """
    cclasstrib_cadastradas: `reformatributaria.classificacaotributaria` + CST vinculado.
    vinculo_ncm / vinculo_produto: contagens por cclasstrib.
    ncms_ativos_linhas: NCMs distintos de produtos ativos (ncm1, ncm2, ncm3).
    """
    hoje = (hoje or date.today()).isoformat()

    vinculo_ncm_count = {_txt(r.get("cclasstrib")): _int(r.get("qtd")) for r in vinculo_ncm}
    vinculo_produto_count = {_txt(r.get("cclasstrib")): _int(r.get("qtd")) for r in vinculo_produto}
    ncms_ativos = sorted({
        _fmt_ncm(r.get("ncm1"), r.get("ncm2"), r.get("ncm3")) for r in ncms_ativos_linhas
    })

    rcur = rfb_conn.cursor()
    rcur.execute(
        "SELECT NCMA_NCM_CD, NCMA_CLTR_ID FROM NCM_APLICAVEL "
        "WHERE NCMA_INICIO_VIGENCIA <= ? AND (NCMA_FIM_VIGENCIA IS NULL OR NCMA_FIM_VIGENCIA >= ?)",
        (hoje, hoje),
    )
    by_prefix = {}
    for cd, cltr_id in rcur.fetchall():
        by_prefix.setdefault(cd, []).append(cltr_id)

    rcur.execute(
        "SELECT ct.CLTR_ID, ct.CLTR_CD, ct.CLTR_DESCRICAO, s.SITR_CD "
        "FROM CLASSIFICACAO_TRIBUTARIA ct JOIN SITUACAO_TRIBUTARIA s ON s.SITR_ID = ct.CLTR_SITR_ID "
        "WHERE ct.CLTR_INICIO_VIGENCIA <= ? AND (ct.CLTR_FIM_VIGENCIA IS NULL OR ct.CLTR_FIM_VIGENCIA >= ?)",
        (hoje, hoje),
    )
    cltr_info = {row[0]: {"cd": row[1], "descricao": row[2], "cst": row[3]}
                 for row in rcur.fetchall()}
    cltr_by_cd = {v["cd"]: v for v in cltr_info.values()}
    rcur.close()

    validacao = []
    for linha in cclasstrib_cadastradas:
        cd = _txt(linha.get("cclasstrib"))
        desc_vr = _txt(linha.get("descricao"))
        cst_vr = _txt(linha.get("cst"))
        aliquotazero = _bool(linha.get("aliquotazero"))
        cst_grupoibscbs = _bool(linha.get("grupoibscbs"))
        of = cltr_by_cd.get(cd)

        # manual do VR: alíquota zero só deve ser marcada em operação que zere
        # 100% do IBS/CBS (Imunidades e Suspensões) — logo, com CST fora do
        # grupo IBSCBS de tributação
        if aliquotazero and cst_grupoibscbs:
            status = (
                "ERRO: Alíquota Zero marcada, mas o CST vinculado não é de "
                "Imunidade/Suspensão (deveria estar relacionada em Imunidades e Suspensões)"
            )
        elif of is None:
            status = "CÓDIGO NÃO EXISTE NA BASE OFICIAL VIGENTE"
        elif of["cst"] != cst_vr:
            status = f"CST DIVERGENTE (oficial={of['cst']}, vr={cst_vr})"
        else:
            status = "OK"

        validacao.append({
            "cclasstrib": cd, "descricao_vr": desc_vr, "cst_vr": cst_vr,
            "descricao_oficial": of["descricao"] if of else None,
            "cst_oficial": of["cst"] if of else None,
            "status": status,
            "qtd_ncm_vinculado": vinculo_ncm_count.get(cd, 0),
            "qtd_produto_vinculado": vinculo_produto_count.get(cd, 0),
        })

    # quais CClassTrib o sortimento real do cliente exigiria, via NCM_APLICAVEL
    # (a tabela oficial casa por prefixo de NCM, daí os cortes 2/4/6/7/8)
    aplicaveis = set()
    ncms_por_cclasstrib: dict[str, set[str]] = {}
    for ncm in ncms_ativos:
        for length in (2, 4, 6, 7, 8):
            for cltr_id in by_prefix.get(ncm[:length], []):
                if cltr_id in cltr_info:
                    cd = cltr_info[cltr_id]["cd"]
                    aplicaveis.add(cd)
                    ncms_por_cclasstrib.setdefault(cd, set()).add(ncm)

    cadastrados_cd = {_txt(v.get("cclasstrib")) for v in cclasstrib_cadastradas}
    faltando_no_sortimento = sorted(aplicaveis - cadastrados_cd)

    return {
        "cclasstrib_cadastrados": validacao,
        "cclasstrib_aplicaveis_pelo_sortimento_nao_cadastrados": [
            {"cclasstrib": cd, "descricao": cltr_by_cd[cd]["descricao"],
             "cst": cltr_by_cd[cd]["cst"],
             "ncms_vinculados": sorted(ncms_por_cclasstrib.get(cd, ()))}
            for cd in faltando_no_sortimento
        ],
        "total_ncms_ativos_analisados": len(ncms_ativos),
    }


def validar_lista_manual(rfb_conn, codigos, cadastrados_no_vr=None, hoje=None):
    """Confere uma lista de CClassTrib informada pelo usuário contra a base
    oficial e marca quais já estão cadastradas no VR."""
    hoje = (hoje or date.today()).isoformat()
    cadastrados_no_vr = cadastrados_no_vr or set()
    rcur = rfb_conn.cursor()
    resultado = []
    for cd in codigos:
        cadastrado = cd in cadastrados_no_vr
        rcur.execute(
            "SELECT ct.CLTR_ID, ct.CLTR_DESCRICAO, s.SITR_CD, s.SITR_DESCRICAO "
            "FROM CLASSIFICACAO_TRIBUTARIA ct JOIN SITUACAO_TRIBUTARIA s ON s.SITR_ID=ct.CLTR_SITR_ID "
            "WHERE ct.CLTR_CD=? AND ct.CLTR_INICIO_VIGENCIA<=? AND (ct.CLTR_FIM_VIGENCIA IS NULL OR ct.CLTR_FIM_VIGENCIA>=?)",
            (cd, hoje, hoje),
        )
        row = rcur.fetchone()
        if not row:
            resultado.append({
                "cclasstrib": cd, "status": "NÃO ENCONTRADO NA BASE OFICIAL VIGENTE",
                "cadastrado_no_vr": cadastrado,
            })
            continue
        cltr_id, desc, cst_cd, cst_desc = row
        rcur.execute(
            "SELECT t.TBTO_SIGLA, pr.PERE_VALOR FROM PERCENTUAL_REDUCAO pr "
            "JOIN TRIBUTO t ON t.TBTO_ID = pr.PERE_TBTO_ID "
            "WHERE pr.PERE_CLTR_ID=? AND pr.PERE_INICIO_VIGENCIA<=? AND (pr.PERE_FIM_VIGENCIA IS NULL OR pr.PERE_FIM_VIGENCIA>=?)",
            (cltr_id, hoje, hoje),
        )
        reducoes = [{"tributo": t, "percentual": v} for t, v in rcur.fetchall()]
        resultado.append({
            "cclasstrib": cd, "descricao_oficial": desc, "cst": cst_cd,
            "cst_descricao": cst_desc, "reducoes": reducoes,
            "status": "OK" if cadastrado else "PENDENTE - NÃO CADASTRADO NO VR",
            "cadastrado_no_vr": cadastrado,
        })
    rcur.close()
    return resultado


# ---------------------------------------------------------------------------
# Análise 4/5/6 - UF, Município e CBS vigentes no ano corrente
# ---------------------------------------------------------------------------

TBTO_ID_CBS = 2
TBTO_ID_IBSUF = 3
TBTO_ID_IBSMUN = 4


def _ids_com_sobreposicao(registros):
    """registros: (grupo, id, datainicio, datatermino) só de cadastros ativos.
    Devolve os id que se sobrepõem a outro registro do MESMO grupo (mesma
    UF/município, ou grupo único no caso do CBS)."""
    por_grupo = {}
    for grupo, id_, di, df in registros:
        if di is None:
            continue
        por_grupo.setdefault(grupo, []).append((di, df, id_))

    sobrepostos = set()
    for _grupo, periodos in por_grupo.items():
        periodos.sort(key=lambda p: p[0])
        fim_max = None
        id_fim_max = None
        for di, df, id_ in periodos:
            if fim_max is not None and di <= fim_max:
                sobrepostos.add(id_)
                sobrepostos.add(id_fim_max)
            df_cmp = df or date(9999, 12, 31)
            if fim_max is None or df_cmp > fim_max:
                fim_max = df_cmp
                id_fim_max = id_
    return sobrepostos


def _carregar_vigencias_oficiais(rfb_conn, tbto_id):
    """ALIQUOTA_REFERENCIA da calculadora RFB: percentual oficial por período,
    por tributo. Devolve [(inicio, fim, valor)]."""
    rcur = rfb_conn.cursor()
    rcur.execute(
        "SELECT ALRE_VALOR, ALRE_INICIO_VIGENCIA, ALRE_FIM_VIGENCIA FROM ALIQUOTA_REFERENCIA "
        "WHERE ALRE_TBTO_ID = ? ORDER BY ALRE_INICIO_VIGENCIA;",
        (tbto_id,),
    )
    periodos = []
    for valor, di, df in rcur.fetchall():
        periodos.append((_data(di), _data(df), valor))
    rcur.close()
    return periodos


def _status_vigencia_oficial(di, df, pct, periodos_oficiais):
    """Confere período/percentual cadastrado no VR contra a alíquota de
    referência oficial vigente na data de início. None = OK."""
    if di is None:
        return None
    periodo = next(
        (p for p in periodos_oficiais if p[0] and p[0] <= di and (p[1] is None or di <= p[1])),
        None,
    )
    if periodo is None:
        return "ERRO: data de início não corresponde a nenhum período oficial de vigência da alíquota"
    p_di, p_df, p_valor = periodo
    if pct is not None and abs(float(pct) - float(p_valor)) > 0.005:
        return (
            f"ERRO: percentual {pct}% não confere com o oficial {p_valor}% "
            f"vigente de {p_di} a {p_df if p_df else 'indeterminado'}"
        )
    if p_df is not None:
        if df is None:
            return f"ERRO: sem data fim cadastrada, mas a alíquota oficial vigente muda após {p_df}"
        if df != p_df:
            return f"ERRO: data fim ({df}) não confere com o fim do período oficial vigente ({p_df})"
    return None


def analise_uf_municipio_cbs(estados, ibs_estadual, ibs_municipal, municipios, cbs,
                             rfb_conn, ano=None):
    """
    estados: public.estado (id, sigla, descricao), sem 'EX'.
    ibs_estadual / ibs_municipal: cadastros ATIVOS de reformatributaria.
    municipios: todos os municípios + flag `cadastrado`.
    cbs: public.cbs, inclusive os excluídos (o status distingue).
    """
    ano = ano or date.today().year
    ini_ano, fim_ano = date(ano, 1, 1), date(ano, 12, 31)

    vig_ibsuf = _carregar_vigencias_oficiais(rfb_conn, TBTO_ID_IBSUF)
    vig_ibsmun = _carregar_vigencias_oficiais(rfb_conn, TBTO_ID_IBSMUN)
    vig_cbs = _carregar_vigencias_oficiais(rfb_conn, TBTO_ID_CBS)

    def vigente_no_ano(di, df):
        return di is not None and di <= fim_ano and (df is None or df >= ini_ano)

    # ---- UF ----
    registros_uf = []
    for linha in ibs_estadual:
        registros_uf.append({
            "id": _txt(linha.get("id")),
            "id_estado": _txt(linha.get("id_estado")),
            "porcentagem": _float(linha.get("porcentagem")),
            "di": _data(linha.get("datainicio")),
            "df": _data(linha.get("datatermino")),
        })
    sobrepostos_uf = _ids_com_sobreposicao(
        [(r["id_estado"], r["id"], r["di"], r["df"]) for r in registros_uf]
    )

    estado_por_id = {
        _txt(e.get("id")): (_txt(e.get("sigla")), _txt(e.get("descricao"))) for e in estados
    }
    uf_status = []
    ufs_com_registro = set()
    for r in registros_uf:
        sigla, desc = estado_por_id.get(r["id_estado"], (r["id_estado"], ""))
        ufs_com_registro.add(r["id_estado"])
        erro_vigencia = _status_vigencia_oficial(r["di"], r["df"], r["porcentagem"], vig_ibsuf)
        if r["id"] in sobrepostos_uf:
            status = "ERRO: sobreposição de período com outro registro desta UF"
        elif erro_vigencia:
            status = erro_vigencia
        elif not vigente_no_ano(r["di"], r["df"]):
            status = "CADASTRADO MAS NÃO VIGENTE NO ANO"
        else:
            status = "OK"
        uf_status.append({
            "sigla": sigla, "descricao": desc, "porcentagem": r["porcentagem"],
            "datainicio": _iso(r["di"]), "datatermino": _iso(r["df"]), "status": status,
        })

    uf_faltando = []
    for e in estados:
        id_estado = _txt(e.get("id"))
        # sem NENHUM cadastro vigente no ano — não basta existir registro
        vigentes = [
            r for r in registros_uf
            if r["id_estado"] == id_estado and vigente_no_ano(r["di"], r["df"])
        ]
        if vigentes:
            continue
        uf_faltando.append({
            "uf_id": id_estado, "sigla": _txt(e.get("sigla")), "descricao": _txt(e.get("descricao")),
        })
        if id_estado not in ufs_com_registro:
            uf_status.append({
                "sigla": _txt(e.get("sigla")), "descricao": _txt(e.get("descricao")),
                "porcentagem": None, "datainicio": None, "datatermino": None,
                "status": "SEM CADASTRO",
            })
    uf_status.sort(key=lambda x: x["sigla"])

    # ---- Município ----
    registros_mun = []
    for linha in ibs_municipal:
        registros_mun.append({
            "id": _txt(linha.get("id")),
            "id_municipio": _txt(linha.get("id_municipio")),
            "municipio": _txt(linha.get("municipio")),
            "sigla": _txt(linha.get("sigla")),
            "porcentagem": _float(linha.get("porcentagem")),
            "di": _data(linha.get("datainicio")),
            "df": _data(linha.get("datatermino")),
        })
    sobrepostos_mun = _ids_com_sobreposicao(
        [(r["id_municipio"], r["id"], r["di"], r["df"]) for r in registros_mun]
    )

    municipio_registros = []
    for r in sorted(registros_mun, key=lambda x: (x["sigla"], x["municipio"])):
        erro_vigencia = _status_vigencia_oficial(r["di"], r["df"], r["porcentagem"], vig_ibsmun)
        if r["id"] in sobrepostos_mun:
            status = "ERRO: sobreposição de período com outro registro deste município"
        elif erro_vigencia:
            status = erro_vigencia
        elif not vigente_no_ano(r["di"], r["df"]):
            status = "CADASTRADO MAS NÃO VIGENTE NO ANO"
        else:
            status = "OK"
        municipio_registros.append({
            "municipio": r["municipio"], "sigla": r["sigla"], "porcentagem": r["porcentagem"],
            "datainicio": _iso(r["di"]), "datatermino": _iso(r["df"]), "status": status,
        })

    # UF sem nenhum município cadastrado = "ainda não configurado" (agregado);
    # UF com alguns cadastrados e outros não = inconsistência item a item.
    por_uf = {}
    for m in municipios:
        sigla = _txt(m.get("sigla"))
        g = por_uf.setdefault(sigla, {"total": 0, "cadastrados": 0, "faltantes": []})
        g["total"] += 1
        if _bool(m.get("cadastrado")):
            g["cadastrados"] += 1
        else:
            g["faltantes"].append(_txt(m.get("municipio")))

    municipio_faltando = []
    municipio_inconsistente = []
    for sigla, g in sorted(por_uf.items()):
        if not g["faltantes"]:
            continue
        if g["cadastrados"] == 0:
            municipio_faltando.append({"sigla": sigla, "qtd_sem_vigencia": len(g["faltantes"])})
        else:
            for desc in g["faltantes"]:
                municipio_inconsistente.append({
                    "sigla": sigla, "municipio": desc,
                    "status": ("INCONSISTÊNCIA: esta UF já possui outro(s) município(s) "
                               "cadastrado(s), mas este não está"),
                })

    # ---- CBS ----
    registros_cbs = []
    for linha in cbs:
        registros_cbs.append({
            "id": _txt(linha.get("id")),
            "porcentagem": _float(linha.get("porcentagem")),
            "di": _data(linha.get("datainicio")),
            "df": _data(linha.get("datatermino")),
            "ativo": _int(linha.get("id_situacaocadastro")) == 1,
        })
    sobrepostos_cbs = _ids_com_sobreposicao(
        [("cbs", r["id"], r["di"], r["df"]) for r in registros_cbs if r["ativo"]]
    )

    cbs_com_status = []
    for r in registros_cbs:
        erro_vigencia = (
            _status_vigencia_oficial(r["di"], r["df"], r["porcentagem"], vig_cbs)
            if r["ativo"] else None
        )
        if r["ativo"] and r["id"] in sobrepostos_cbs:
            status = "ERRO: sobreposição de período com outro registro de CBS"
        elif not r["ativo"]:
            status = "EXCLUÍDO"
        elif erro_vigencia:
            status = erro_vigencia
        elif not vigente_no_ano(r["di"], r["df"]):
            status = "CADASTRADO MAS NÃO VIGENTE NO ANO"
        else:
            status = "OK"
        cbs_com_status.append({
            "id": r["id"], "porcentagem": r["porcentagem"], "datainicio": _iso(r["di"]),
            "datatermino": _iso(r["df"]), "ativo": r["ativo"], "status": status,
        })

    return {
        "ano_referencia": ano,
        "uf_sem_ibsestadual_vigente": uf_faltando,
        "municipio_sem_ibsmunicipal_vigente": municipio_faltando,
        "municipio_inconsistente": municipio_inconsistente,
        "cbs_registros": cbs_com_status,
        "cbs_vigente_no_ano": any(r["ativo"] and vigente_no_ano(r["di"], r["df"]) for r in registros_cbs),
        "uf_status": uf_status,
        "municipio_registros": municipio_registros,
    }


# ---------------------------------------------------------------------------
# Análise 7 - vínculo produto/NCM x base oficial
# ---------------------------------------------------------------------------

def analise_vinculo(produtos, vinculo_produto_linhas, vinculo_ncm_linhas,
                    classificacoes, rfb_conn, hoje=None, limite_amostra=200):
    """
    produtos: produtos ativos por loja (id_produto, ncm1..3, ean, id_loja, descricao).
    vinculo_produto_linhas / vinculo_ncm_linhas: tabelas de vínculo do VR.
    classificacoes: id, cclasstrib, descricao, cst.
    """
    hoje = (hoje or date.today()).isoformat()

    vinculo_produto = {
        (_txt(r.get("id_produto")), _txt(r.get("id_loja"))): _txt(r.get("id_classificacao"))
        for r in vinculo_produto_linhas
    }

    vinculo_ncm = {}
    ncm_classificacoes = {}  # (ncm, loja) -> {classificações} — pega duplicidade
    for r in vinculo_ncm_linhas:
        chave = (_fmt_ncm(r.get("ncm1"), r.get("ncm2"), r.get("ncm3")), _txt(r.get("id_loja")))
        id_class = _txt(r.get("id_classificacao"))
        vinculo_ncm[chave] = id_class
        ncm_classificacoes.setdefault(chave, set()).add(id_class)
    ncm_duplicado = {c for c, classes in ncm_classificacoes.items() if len(classes) > 1}

    classificacao_info = {
        _txt(r.get("id")): {
            "cclasstrib": _txt(r.get("cclasstrib")),
            "descricao": _txt(r.get("descricao")),
            "cst": _txt(r.get("cst")),
        }
        for r in classificacoes
    }

    rcur = rfb_conn.cursor()
    rcur.execute(
        "SELECT NCMA_NCM_CD, NCMA_CLTR_ID FROM NCM_APLICAVEL "
        "WHERE NCMA_INICIO_VIGENCIA <= ? AND (NCMA_FIM_VIGENCIA IS NULL OR NCMA_FIM_VIGENCIA >= ?)",
        (hoje, hoje),
    )
    by_prefix = {}
    for cd, cltr_id in rcur.fetchall():
        by_prefix.setdefault(cd, []).append(cltr_id)
    rcur.execute(
        "SELECT CLTR_ID, CLTR_CD FROM CLASSIFICACAO_TRIBUTARIA "
        "WHERE CLTR_INICIO_VIGENCIA <= ? AND (CLTR_FIM_VIGENCIA IS NULL OR CLTR_FIM_VIGENCIA >= ?)",
        (hoje, hoje),
    )
    cltr_cd_by_id = dict(rcur.fetchall())
    rcur.close()

    cache_esperado: dict[str, set] = {}

    def esperado_para_ncm(ncm):
        if ncm in cache_esperado:
            return cache_esperado[ncm]
        esperado = set()
        for length in (2, 4, 6, 7, 8):
            for cltr_id in by_prefix.get(ncm[:length], []):
                if cltr_id in cltr_cd_by_id:
                    esperado.add(cltr_cd_by_id[cltr_id])
        cache_esperado[ncm] = esperado
        return esperado

    contagem = {"SEM_VINCULO": 0, "VINCULO_POR_PRODUTO": 0, "VINCULO_POR_NCM": 0}
    divergencias = []
    sem_vinculo = []
    vinculado_por_produto = []
    vinculado_por_ncm = []

    for p in produtos:
        id_produto = _txt(p.get("id_produto"))
        id_loja = _txt(p.get("id_loja"))
        ean = _txt(p.get("ean"))
        descricao = _txt(p.get("descricao"))
        ncm = _fmt_ncm(p.get("ncm1"), p.get("ncm2"), p.get("ncm3"))
        chave_prod = (id_produto, id_loja)

        if chave_prod in vinculo_produto:
            tipo = "VINCULO_POR_PRODUTO"
            info = classificacao_info.get(vinculo_produto[chave_prod], {})
        elif (ncm, id_loja) in vinculo_ncm:
            tipo = "VINCULO_POR_NCM"
            info = classificacao_info.get(vinculo_ncm[(ncm, id_loja)], {})
        else:
            tipo = "SEM_VINCULO"
            info = {}

        cclasstrib_usado = info.get("cclasstrib")
        contagem[tipo] += 1
        esperado = esperado_para_ncm(ncm)

        if tipo == "VINCULO_POR_NCM" and (ncm, id_loja) in ncm_duplicado:
            status_linha = ("ERRO: NCM cadastrado em mais de uma Classificação Tributária "
                            "para esta loja (manual 6.2.1 - não deve haver duplicidade)")
        else:
            status_linha = "OK"

        linha_base = {
            "id_produto": id_produto, "ean": ean, "descricao": descricao, "ncm": ncm,
            "id_loja": id_loja, "cclasstrib": cclasstrib_usado,
            "cclasstrib_descricao": info.get("descricao"), "cst": info.get("cst"),
            "status": status_linha,
        }

        if tipo == "SEM_VINCULO":
            sem_vinculo.append({
                "id_produto": id_produto, "ean": ean, "descricao": descricao, "ncm": ncm,
                "id_loja": id_loja, "cclasstrib_esperado": sorted(esperado) or None,
            })
        elif tipo == "VINCULO_POR_PRODUTO":
            vinculado_por_produto.append(linha_base)
        else:
            vinculado_por_ncm.append(linha_base)

        if tipo != "SEM_VINCULO" and esperado and cclasstrib_usado not in esperado:
            divergencias.append({
                "id_produto": id_produto, "ean": ean, "descricao": descricao, "ncm": ncm,
                "id_loja": id_loja, "tipo_vinculo": tipo,
                "cclasstrib_usado": cclasstrib_usado, "cclasstrib_esperado": sorted(esperado),
            })

    return {
        "total_produtos_ativos_analisados": len(produtos),
        "contagem_por_tipo_vinculo": contagem,
        "total_sem_vinculo": len(sem_vinculo),
        "total_possivel_erro_classificacao": len(divergencias),
        "vinculado_por_produto_completo": vinculado_por_produto,
        "vinculado_por_ncm_completo": vinculado_por_ncm,
        "amostra_sem_vinculo": sem_vinculo[:limite_amostra],
        "amostra_possivel_erro_classificacao": divergencias[:limite_amostra],
    }


def aplicar_contagem_efetiva_cclasstrib(analise_3, analise_7):
    """Troca a contagem bruta das tabelas de cadastro pela contagem EFETIVA:
    produtos ativos e NCMs distintos que, depois da precedência (vínculo por
    produto vence vínculo por NCM), realmente são tributados com aquele
    cClassTrib no ERP do cliente."""
    produtos_por_cd = {}
    ncms_por_cd = {}
    for linha in (analise_7["vinculado_por_produto_completo"]
                  + analise_7["vinculado_por_ncm_completo"]):
        cd = linha.get("cclasstrib")
        if not cd:
            continue
        produtos_por_cd.setdefault(cd, set()).add(linha["id_produto"])
        ncms_por_cd.setdefault(cd, set()).add(linha["ncm"])
    for c in analise_3["cclasstrib_cadastrados"]:
        cd = c["cclasstrib"]
        c["qtd_ncm_vinculado"] = len(ncms_por_cd.get(cd, ()))
        c["qtd_produto_vinculado"] = len(produtos_por_cd.get(cd, ()))


def enxugar_vinculo(analise_7, limite=200):
    """As listas completas de vínculo existem só para a contagem efetiva acima
    (podem passar de dezenas de milhares de linhas). Depois de usá-las, ficam
    só as amostras — o resultado vai inteiro para uma coluna JSONB."""
    analise_7["vinculado_por_produto"] = analise_7["vinculado_por_produto_completo"][:limite]
    analise_7["vinculado_por_ncm"] = analise_7["vinculado_por_ncm_completo"][:limite]
    analise_7.pop("vinculado_por_produto_completo", None)
    analise_7.pop("vinculado_por_ncm_completo", None)
    return analise_7


# ---------------------------------------------------------------------------
# Análise 8 - notas de débito/crédito
# ---------------------------------------------------------------------------

# Códigos válidos por tipo, conforme NT 2025.002-RTC v1.36 (mesma lista do
# manual "VRNS - FIS - Cadastro dos Tipos NF Credito e Debito.pdf")
CODIGOS_DEBITO_VALIDOS = {
    "01": "Transferência de créditos para Cooperativas",
    "02": "Anulação de Crédito por Saídas Imunes/Isentas",
    "03": "Débitos de notas fiscais não processadas na apuração",
    "04": "Multa e juros",
    "05": "Transferência de crédito na sucessão",
    "06": "Pagamento antecipado",
    "07": "Perda em estoque (Perecimento, Perda, Furto, Roubo)",
    "08": "Desenquadramento do SN",
}
CODIGOS_CREDITO_VALIDOS = {
    "01": "Multa e juros",
    "02": "Apropriação de crédito presumido de IBS sobre o saldo devedor na ZFM",
    "03": "Retorno por recusa total na entrega ou por não localização do destinatário",
    "04": "Redução de valores",
    "05": "Transferência de crédito na sucessão",
    "06": "Retorno por recusa parcial na entrega",
}


def _vinculo_cfoptiposaida(linha_cfop: dict, tiposaida: dict, campo: str):
    """O vínculo (débito/crédito ou cClassTrib) pode estar no Tipo de Saída
    inteiro ou sobrescrito na linha de CFOP — e a coluna em `cfoptiposaida`
    nem existe em toda instalação. Vale a da linha de CFOP quando houver."""
    valor_cfop = _txt(linha_cfop.get(campo))
    if valor_cfop:
        return valor_cfop
    return _txt(tiposaida.get(campo))


def analise_debito_credito(tipodebitocredito, debitocredito, tiposaida, cfoptiposaida):
    registros = []
    for linha in debitocredito:
        cod_xml = _txt(linha.get("cod_xml"))
        cod_str = f"{int(cod_xml):02d}" if cod_xml.isdigit() else None
        sigla_up = _txt(linha.get("sigla")).upper()
        tipo_desc = _txt(linha.get("tipo_descricao"))
        tabela_valida = CODIGOS_DEBITO_VALIDOS if sigla_up.startswith("D") else CODIGOS_CREDITO_VALIDOS
        if cod_str not in tabela_valida:
            status = f"ERRO: código {cod_str} não previsto na NT 2025.002 v1.36 para tipo {tipo_desc}"
        else:
            status = "OK"
        registros.append({
            "tipo": tipo_desc, "cod_xml": cod_str, "descricao": _txt(linha.get("descricao")),
            "ativo": _int(linha.get("id_situacaocadastro")) == 1, "status": status,
        })

    ts_por_id = {_txt(t.get("id")): t for t in tiposaida}
    cfop_por_ts = {}
    for linha in cfoptiposaida:
        cfop_por_ts.setdefault(_txt(linha.get("id_tiposaida")), []).append(_json(linha.get("linha")))

    vinculados = set()
    for id_ts, ts in ts_por_id.items():
        if _txt(ts.get("id_debitocredito")):
            vinculados.add(id_ts)
            continue
        if any(_txt(l.get("id_debitocredito")) for l in cfop_por_ts.get(id_ts, [])):
            vinculados.add(id_ts)

    qtd_tiposaida_ativo = sum(1 for t in tiposaida if _int(t.get("id_situacaocadastro")) == 1)

    return {
        "tipodebitocredito_cadastrados": len(tipodebitocredito),
        "debitocredito_cadastrados": len(debitocredito),
        "tiposaida_ativos": qtd_tiposaida_ativo,
        "tiposaida_com_vinculo_debitocredito": len(vinculados),
        "configurado": (len(tipodebitocredito) > 0 and len(debitocredito) > 0
                        and len(vinculados) > 0),
        "registros": registros,
    }


# ---------------------------------------------------------------------------
# Análise 11 - Tipo de Saída (regras por CFOP)
# ---------------------------------------------------------------------------
#
# Três regras do campo "Reforma Tributária" da aba "Dados Fiscais" do cadastro
# de Tipo de Saída:
#
# 1) Baixa de estoque por perda (CFOP 5.927/6.927) → Débito 07 "Perda em
#    estoque" (NT 2025.002 v1.36).
# 2) Transferência entre estabelecimentos do mesmo titular → cClassTrib 410002.
# 3) Bonificação, doação ou brinde (5.910/6.910) → um entre 410001, 410003 e
#    410026 (a escolha é decisão fiscal do cliente, não há um único certo).
#
# A checagem é sempre do CADASTRO: um Tipo de Saída mal configurado é erro
# mesmo que nunca tenha sido usado. O movimento real (escrituração fiscal)
# entra como informação complementar por linha e como uma checagem à parte —
# "operação sem cadastro", lançamento com CFOP do grupo e sem NENHUM Tipo de
# Saída vinculado, que só o cruzamento com o movimento enxerga.

CFOP_BAIXA_ESTOQUE_PERDA = ["5927", "6927"]
CFOP_TRANSFERENCIA_MERCADORIA = ["5151", "6151", "5152", "6152", "5409", "6409"]
CFOP_BONIFICACAO_DOACAO_BRINDE = ["5910", "6910"]

ORIENTACAO_CCLASSTRIB_BONIFICACAO = (
    "410001 = bonificação constante no próprio documento fiscal, sem depender de "
    "evento posterior; 410003 = doação sem contraprestação em benefício do doador; "
    "410026 = doação com anulação de crédito"
)


def _so_digitos(texto):
    return re.sub(r"[^0-9]", "", texto or "")


def _regra_tiposaida_por_cfop(cfoptiposaida, ts_por_id, lista_cfop, campo,
                              alvos_validos, descricao_alvo, movimento,
                              operacoes_sem_cadastro_por_cfop, orientacao=None):
    operacoes_sem_cadastro = sum(operacoes_sem_cadastro_por_cfop.get(c, 0) for c in lista_cfop)

    linhas = [
        l for l in cfoptiposaida
        if _so_digitos(_txt(l.get("cfop"))) in lista_cfop
        and _int(ts_por_id.get(_txt(l.get("id_tiposaida")), {}).get("id_situacaocadastro")) == 1
    ]
    if not linhas:
        return {
            "cadastro": [],
            "nenhum_tiposaida_cadastrado": True,
            "operacoes_sem_cadastro": operacoes_sem_cadastro,
        }

    cadastro = []
    vistos = set()
    for l in linhas:
        id_ts = _txt(l.get("id_tiposaida"))
        cfop = _txt(l.get("cfop"))
        if (id_ts, cfop) in vistos:
            continue
        vistos.add((id_ts, cfop))
        ts = ts_por_id.get(id_ts, {})
        valor_campo = _vinculo_cfoptiposaida(_json(l.get("linha")), ts, campo)
        qtd_mov = movimento.get((id_ts, _so_digitos(cfop)), 0)

        if not alvos_validos:
            status = f"ERRO: {descricao_alvo} não está cadastrado no banco"
        elif not valor_campo:
            status = f"ERRO: sem vínculo no campo Reforma Tributária (deveria ser {descricao_alvo})"
            if orientacao:
                status += f" | {orientacao}"
        elif valor_campo not in alvos_validos:
            status = f"ERRO: vinculado a um código diferente de {descricao_alvo}"
        else:
            status = "OK"

        cadastro.append({
            "id_tiposaida": id_ts, "descricao": _txt(ts.get("descricao")), "cfop": cfop,
            "descricao_cfop": _txt(l.get("descricao_cfop")),
            "movimentou": qtd_mov > 0, "qtd_movimento": qtd_mov, "status": status,
        })

    cadastro.sort(key=lambda x: (x["id_tiposaida"], x["cfop"]))
    return {
        "cadastro": cadastro,
        "nenhum_tiposaida_cadastrado": False,
        "operacoes_sem_cadastro": operacoes_sem_cadastro,
    }


def analise_tiposaida(tiposaida, cfoptiposaida, debitocredito, cclasstrib_cadastradas,
                      movimento_linhas=None, sem_cadastro_linhas=None):
    """
    movimento_linhas / sem_cadastro_linhas: cruzamento com a escrituração fiscal
    real; opcionais — sem eles a regra de cadastro continua valendo, só não há
    a informação de "chegou a ser usado" nem a checagem de operação sem cadastro.
    """
    ts_por_id = {_txt(t.get("id")): t for t in tiposaida}

    # os alvos não são id fixo: variam de banco para banco, então são
    # localizados pelo código de negócio
    alvo_debito_perda = {
        _txt(d.get("id")) for d in debitocredito
        if _txt(d.get("cod_xml")).lstrip("0") == "7"
        and _txt(d.get("sigla")).upper().startswith("D")
        and _int(d.get("id_situacaocadastro")) == 1
    }
    alvo_transferencia = {
        _txt(c.get("id")) for c in cclasstrib_cadastradas
        if _txt(c.get("cclasstrib")) == "410002"
        and _int(c.get("id_situacaocadastro"), 1) == 1
    }
    alvos_bonificacao = {
        _txt(c.get("id")) for c in cclasstrib_cadastradas
        if _txt(c.get("cclasstrib")) in ("410001", "410003", "410026")
        and _int(c.get("id_situacaocadastro"), 1) == 1
    }

    movimento = {
        (_txt(m.get("id_tiposaida")), _so_digitos(_txt(m.get("cfop")))): _int(m.get("qtd"))
        for m in (movimento_linhas or [])
    }
    sem_cadastro = {
        _so_digitos(_txt(s.get("cfop"))): _int(s.get("qtd"))
        for s in (sem_cadastro_linhas or [])
    }

    baixa_estoque = _regra_tiposaida_por_cfop(
        cfoptiposaida, ts_por_id, CFOP_BAIXA_ESTOQUE_PERDA, "id_debitocredito",
        alvo_debito_perda, "Débito 07 - Perda em estoque", movimento, sem_cadastro,
    )
    transferencia = _regra_tiposaida_por_cfop(
        cfoptiposaida, ts_por_id, CFOP_TRANSFERENCIA_MERCADORIA, "id_classificacaotributaria",
        alvo_transferencia, "cClassTrib 410002 (transferência entre estabelecimentos)",
        movimento, sem_cadastro,
    )
    bonificacao = _regra_tiposaida_por_cfop(
        cfoptiposaida, ts_por_id, CFOP_BONIFICACAO_DOACAO_BRINDE, "id_classificacaotributaria",
        alvos_bonificacao, "um cClassTrib de bonificação/doação (410001, 410003 ou 410026)",
        movimento, sem_cadastro, orientacao=ORIENTACAO_CCLASSTRIB_BONIFICACAO,
    )

    return {
        "debito_perda_encontrado": bool(alvo_debito_perda),
        "cclasstrib_transferencia_encontrado": bool(alvo_transferencia),
        "cclasstrib_bonificacao_encontrado": bool(alvos_bonificacao),
        "movimento_disponivel": movimento_linhas is not None,
        "baixa_estoque": baixa_estoque,
        "transferencia": transferencia,
        "bonificacao": bonificacao,
    }


# ---------------------------------------------------------------------------
# Análise 10 - Eventos de IBS/CBS (NT 2025.002-RTC, item 8.1, pág. 75)
# ---------------------------------------------------------------------------

# código -> (descrição oficial, palavra-chave do autor esperado)
EVENTOS_OFICIAIS = {
    112110: ("Informação de efetivo pagamento integral para liberar crédito presumido do adquirente", "emitente"),
    112120: ("Importação em ALC/ZFM não convertida em isenção", "emitente"),
    112130: ("Perecimento, perda, roubo ou furto durante o transporte contratado pelo fornecedor", "emitente"),
    112140: ("Fornecimento não realizado com pagamento antecipado", "emitente"),
    112150: ("Atualização da Data de Previsão de Entrega", "emitente"),
    211110: ("Solicitação de Apropriação de crédito presumido", "destinat"),
    211124: ("Perecimento, perda, roubo ou furto durante o transporte contratado pelo adquirente", "destinat"),
    211128: ("Aceite de débito na apuração por emissão de nota de crédito", "destinat"),
    211130: ("Imobilização de Item", "destinat"),
    211140: ("Solicitação de Apropriação de Crédito de Combustível", "destinat"),
    211150: ("Solicitação de Apropriação de Crédito para bens e serviços que dependem de atividade do adquirente", "destinat"),
    212110: ("Manifestação sobre Pedido de Transferência de Crédito de IBS em Operações de Sucessão", "sucessor"),
    212120: ("Manifestação sobre Pedido de Transferência de Crédito de CBS em Operações de Sucessão", "sucessor"),
    412120: ("Manifestação do Fisco sobre Pedido de Transferência de Crédito de IBS em Operações de Sucessão", "fisco"),
    412130: ("Manifestação do Fisco sobre Pedido de Transferência de Crédito de CBS em Operações de Sucessão", "fisco"),
}
# A NT também cria um "Evento de Cancelamento" genérico, sem código numérico
# fixo no texto — não dá para validar um código específico para ele.


def analise_eventos(tipoautor, tipoevento, totais=None):
    # o tipoevento aponta para o tipoautor por id; o código de negócio vem
    # junto porque nem toda instalação tem id == codigo
    autor_por_id = {_txt(a.get("id")): _txt(a.get("descricao")) for a in tipoautor}
    autor_por_codigo = {_txt(a.get("codigo")): _txt(a.get("descricao")) for a in tipoautor}

    registros = []
    codigos_cadastrados = set()
    for linha in tipoevento:
        codigo = _int(linha.get("codigo"))
        desc_vr = _txt(linha.get("descricao"))
        id_autor = _txt(linha.get("id_tipoautor"))
        codigos_cadastrados.add(codigo)
        desc_autor_vr = autor_por_id.get(id_autor) or autor_por_codigo.get(id_autor, "")

        oficial = EVENTOS_OFICIAIS.get(codigo)
        if oficial is None:
            status = "ERRO: código não previsto na NT 2025.002 (item 8.1)"
        else:
            desc_oficial, autor_esperado = oficial
            _identica, equivalente, _comuns = _descricoes_equivalentes(desc_vr, desc_oficial)
            if autor_esperado not in _normalizar_texto(desc_autor_vr):
                status = (f"ERRO: autor cadastrado ({desc_autor_vr or 'não definido'}) diverge "
                          f"do esperado pela NT ({autor_esperado})")
            elif not equivalente:
                status = "OK (código e autor corretos - descrição divergente, conferir manualmente)"
            else:
                status = "OK"
        registros.append({
            "codigo": codigo, "descricao_vr": desc_vr, "autor_vr": desc_autor_vr, "status": status,
        })

    eventos_faltando = [
        {"codigo": c, "descricao": EVENTOS_OFICIAIS[c][0], "autor": EVENTOS_OFICIAIS[c][1]}
        for c in sorted(set(EVENTOS_OFICIAIS) - codigos_cadastrados)
    ]

    tipoautor_esperados = {"emitente", "destinat", "sucessor", "fisco"}
    tipoautor_presentes = {_normalizar_texto(d) for d in autor_por_id.values()}
    tipoautor_faltando = sorted(
        p for p in tipoautor_esperados
        if not any(p in presente for presente in tipoautor_presentes)
    )

    linha_totais = (totais or [{}])[0] if totais else {}
    return {
        "tipoevento_cadastrados": registros,
        "tipoevento_faltando": eventos_faltando,
        "tipoautor_faltando": tipoautor_faltando,
        "qtd_evento": _int(linha_totais.get("qtd_evento")),
        "qtd_eventoitem": _int(linha_totais.get("qtd_eventoitem")),
    }


# ---------------------------------------------------------------------------
# Análise 9 - parâmetro "Data envio IBS/CBS" (NF Saída x PDV/NFC-e)
# ---------------------------------------------------------------------------

def _parse_data_parametro(valor):
    """O parâmetro é texto livre no VR, gravado em dd/mm/aaaa."""
    texto = _txt(valor)
    if not texto:
        return None
    try:
        d, m, a = texto.split("/")
        return date(int(a), int(m), int(d))
    except (ValueError, TypeError):
        return None


def analise_parametro_data_ibscbs(parametro_nfe, parametro_pdv, cbs):
    """
    parametro_nfe / parametro_pdv: uma linha por loja, com o valor do parâmetro
    (NULL quando a loja não tem valor gravado) e a descrição do parâmetro achado.
    """
    descricao_nfe = next((_txt(r.get("descricao_parametro")) for r in parametro_nfe
                          if _txt(r.get("descricao_parametro"))), None)
    descricao_pdv = next((_txt(r.get("descricao_parametro")) for r in parametro_pdv
                          if _txt(r.get("descricao_parametro"))), None)

    if not descricao_nfe or not descricao_pdv:
        return {
            "descricao_parametro_nfe": descricao_nfe,
            "descricao_parametro_pdv": descricao_pdv,
            "cbs_datainicio": None,
            "lojas": [],
            "tudo_ok": False,
            "erro": ("Parâmetro não encontrado neste banco "
                     "(verificar a descrição em public.parametro / pdv.parametro)."),
        }

    valores_pdv = {_txt(r.get("id_loja")): _txt(r.get("valor")) for r in parametro_pdv}

    # a vigência do CBS é a referência: a data do parâmetro tem que bater com ela
    datas_cbs = sorted(d for d in (_data(r.get("datainicio")) for r in cbs) if d)
    cbs_datainicio = datas_cbs[0] if datas_cbs else None

    linhas = []
    for r in parametro_nfe:
        id_loja = _txt(r.get("id_loja"))
        valor_nfe = _txt(r.get("valor"))
        valor_pdv = valores_pdv.get(id_loja, "")
        data_nfe = _parse_data_parametro(valor_nfe)
        data_pdv = _parse_data_parametro(valor_pdv)

        if not valor_nfe or not valor_pdv:
            status = "PARÂMETRO NÃO PREENCHIDO"
        elif data_nfe != data_pdv:
            status = "DIVERGENTE ENTRE NF SAÍDA E PDV/NFC-e"
        elif cbs_datainicio and data_nfe != cbs_datainicio:
            status = f"DIVERGENTE DA VIGÊNCIA DO CBS ({cbs_datainicio})"
        else:
            status = "OK"

        linhas.append({
            "id_loja": id_loja, "loja": _txt(r.get("loja")),
            "data_nf_saida": valor_nfe or None, "data_pdv_nfce": valor_pdv or None,
            "status": status,
        })

    return {
        "descricao_parametro_nfe": descricao_nfe,
        "descricao_parametro_pdv": descricao_pdv,
        "cbs_datainicio": _iso(cbs_datainicio),
        "lojas": linhas,
        "tudo_ok": all(l["status"] == "OK" for l in linhas) if linhas else False,
    }


# ---------------------------------------------------------------------------
# Empresas / lojas (identidade fiscal)
# ---------------------------------------------------------------------------
#
# public.empresa NÃO é a identidade fiscal da loja (guarda um cadastro de
# franquia/cobrança da própria VR). A identidade fiscal fica em
# public.fornecedor, referenciada por public.loja.id_fornecedor — cada
# loja/filial é cadastrada como um "fornecedor", com razão social e CNPJ.

def _formatar_cnpj(cnpj):
    texto = _so_digitos(_txt(cnpj))
    if not texto:
        return "-"
    digitos = texto.zfill(14)
    return f"{digitos[0:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:14]}"


def listar_empresas(linhas):
    vistos = set()
    empresas = []
    for r in linhas:
        cnpj_fmt = _formatar_cnpj(r.get("cnpj"))
        razao_fmt = _txt(r.get("razaosocial"))
        chave = (razao_fmt, cnpj_fmt)
        if chave in vistos:
            continue
        vistos.add(chave)
        empresas.append({"loja": _txt(r.get("loja")), "razaosocial": razao_fmt, "cnpj": cnpj_fmt})
    return empresas


def resumir(resultado: dict) -> dict:
    """Contagens que a tela usa nos cards de topo, calculadas uma vez aqui para
    não repetir a regra de 'o que conta como problema' no frontend."""
    ncm = resultado.get("analise_1_ncm") or {}
    cst = resultado.get("analise_2_cst") or {}
    ccl = resultado.get("analise_3_cclasstrib") or {}
    manual = resultado.get("analise_3b_lista_manual") or []

    cst_problemas = [c for c in cst.get("cst_cadastrados", []) if not c["status"].startswith("OK")]
    ccl_problemas = [c for c in ccl.get("cclasstrib_cadastrados", []) if c["status"] != "OK"]

    resumo = {
        # os totais vêm dos campos próprios: as listas são truncadas (LIMITE_LISTA)
        "ncm_invalidos": ncm.get("total_ativos_invalidos", 0),
        "ncm_produtos_afetados": ncm.get("total_produtos_ncm_invalido", 0),
        "ncm_faltando": ncm.get("total_faltando_no_banco", 0),
        "cst_com_problema": len(cst_problemas),
        "cst_total": len(cst.get("cst_cadastrados", [])),
        "cclasstrib_com_problema": len(ccl_problemas),
        "cclasstrib_total": len(ccl.get("cclasstrib_cadastrados", [])),
        "cclasstrib_faltando_sortimento": len(
            ccl.get("cclasstrib_aplicaveis_pelo_sortimento_nao_cadastrados", [])
        ),
        "cclasstrib_lista_pendente": len(manual),
    }

    # as análises abaixo podem não ter rodado (tabela inexistente no banco do
    # cliente ou tarefa fora do escopo) — só entram no resumo quando existem
    aliq = resultado.get("analise_4_5_6_uf_municipio_cbs") or {}
    if aliq and not aliq.get("indisponivel"):
        # "SEM CADASTRO" não é divergência de alíquota: um cliente que opera
        # numa UF só tem 26 assim, e misturar isso com erro de percentual/
        # vigência transforma o card num alarme falso permanente.
        resumo["uf_com_erro"] = sum(
            1 for u in aliq.get("uf_status", []) if u["status"].startswith("ERRO")
        )
        resumo["uf_sem_cadastro"] = sum(
            1 for u in aliq.get("uf_status", []) if u["status"] == "SEM CADASTRO"
        )
        resumo["uf_total"] = len(aliq.get("uf_status", []))
        resumo["municipio_com_erro"] = sum(
            1 for m in aliq.get("municipio_registros", []) if m["status"].startswith("ERRO")
        )
        resumo["municipio_inconsistente"] = len(aliq.get("municipio_inconsistente", []))
        resumo["cbs_vigente"] = bool(aliq.get("cbs_vigente_no_ano"))

    vinculo = resultado.get("analise_7_vinculo") or {}
    if vinculo and not vinculo.get("indisponivel"):
        resumo["produtos_analisados"] = vinculo.get("total_produtos_ativos_analisados", 0)
        resumo["produtos_sem_vinculo"] = vinculo.get("total_sem_vinculo", 0)
        resumo["produtos_classificacao_divergente"] = vinculo.get(
            "total_possivel_erro_classificacao", 0
        )

    debcred = resultado.get("analise_8_debito_credito") or {}
    if debcred and not debcred.get("indisponivel"):
        resumo["debito_credito_configurado"] = bool(debcred.get("configurado"))
        resumo["debito_credito_com_problema"] = sum(
            1 for r in debcred.get("registros", []) if r["status"] != "OK"
        )

    parametro = resultado.get("analise_9_parametro_data_ibscbs") or {}
    if parametro and not parametro.get("indisponivel"):
        resumo["parametro_data_ok"] = bool(parametro.get("tudo_ok"))
        resumo["parametro_lojas_com_problema"] = sum(
            1 for l in parametro.get("lojas", []) if l["status"] != "OK"
        )

    eventos = resultado.get("analise_10_eventos") or {}
    if eventos and not eventos.get("indisponivel"):
        resumo["eventos_com_problema"] = sum(
            1 for e in eventos.get("tipoevento_cadastrados", []) if not e["status"].startswith("OK")
        )
        resumo["eventos_faltando"] = len(eventos.get("tipoevento_faltando", []))

    saida = resultado.get("analise_11_tiposaida") or {}
    if saida and not saida.get("indisponivel"):
        resumo["tiposaida_com_problema"] = sum(
            1
            for grupo in ("baixa_estoque", "transferencia", "bonificacao")
            for c in (saida.get(grupo) or {}).get("cadastro", [])
            if c["status"] != "OK"
        )
        resumo["tiposaida_operacoes_sem_cadastro"] = sum(
            (saida.get(grupo) or {}).get("operacoes_sem_cadastro", 0)
            for grupo in ("baixa_estoque", "transferencia", "bonificacao")
        )

    return resumo
