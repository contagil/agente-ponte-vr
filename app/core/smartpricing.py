"""SmartPricing — simulação do preço de venda com a CBS no lugar de PIS/COFINS.

Portado do projeto da Francilene (github.com/francilenediniz/SmartPricing-Reforma-Legal,
app/calculo.py) pra rodar aqui na ponte. SOMENTE LEITURA: lê o VR pelo agente e devolve preços
sugeridos; quem grava no VR é o cliente, importando o TXT que a tela gera.

Regras (definidas pela Francilene em 06/10/2026):
  1. valor sem impostos = preço × (1 − PIS − COFINS − ICMS)        (todos por dentro)
  2. alíquota CBS       = 9,2% × (1 − redução do cClassTrib)       (60% → 3,68%; 100% → 0)
  3. valor com CBS      = valor sem impostos × (1 + alíquota CBS)  (CBS por fora)
  4. preço calculado    = valor com CBS / (1 − ICMS)               (ICMS por dentro)
  5. preço sugerido     = preço calculado arredondado PARA CIMA até o próximo final 9 de centavo
  6. trava de margem    = se a margem bruta CAI no preço sugerido, sugere o preço que mantém a
                          margem bruta original (também com final 9):
                          P = (CBS + custo s/ imp) ÷ (1 − ICMS − margem original)
                          (margem já negativa hoje: sem trava; produto sem custo: sem trava)
A CBS incide sobre a base sem impostos do preço atual e NÃO é refeita depois do final 9 nem da
edição do usuário — o que sobra/falta vira "ajuste" na margem. Sem alíquota de ICMS de
consumidor cadastrada → ICMS 0 e o item é sinalizado (`sem_icms`).
"""
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

ALIQ_CBS = Decimal("0.092")
CEM = Decimal(100)
CENTAVO = Decimal("0.01")
QUATRO_CASAS = Decimal("0.0001")


def _dec(valor) -> Decimal:
    """Os valores chegam do agente como texto; vazio/inválido vira 0."""
    if valor is None:
        return Decimal(0)
    try:
        return Decimal(str(valor).strip().replace(",", ".") or "0")
    except InvalidOperation:
        return Decimal(0)


def _txt(valor) -> str:
    return (str(valor) if valor is not None else "").strip()


def _pct(valor) -> Decimal:
    return _dec(valor) / CEM


def _r(valor: Decimal, casas: Decimal = CENTAVO) -> float:
    return float(valor.quantize(casas, ROUND_HALF_UP))


def final9(preco: Decimal) -> Decimal:
    """Arredonda para cima até o próximo valor com centavo terminado em 9."""
    if preco <= 0:
        return Decimal(0)
    centavos = int((preco * CEM).quantize(Decimal(1), ROUND_CEILING))
    centavos += (9 - centavos % 10) % 10
    return Decimal(centavos) / CEM


def simular(linha: dict) -> dict:
    """Uma linha do agente (produto × loja) → entradas + preço sugerido. As derivações que só
    dependem do preço final (CBS em R$, ICMS, margens) a tela refaz — é o que permite editar o preço."""
    preco = _dec(linha.get("precovenda"))
    pis, cofins = _pct(linha.get("aliq_pis")), _pct(linha.get("aliq_cofins"))
    icms = _pct(linha.get("aliq_icms_efetiva"))
    reducao = _pct(linha.get("reducao_ibscbs"))
    aliq_cbs = ALIQ_CBS * (1 - reducao)
    cci, csi = _dec(linha.get("custocomimposto")), _dec(linha.get("custosemimposto"))

    sem_impostos = preco * (1 - pis - cofins - icms)
    valor_cbs = sem_impostos * aliq_cbs
    preco_calculado = (sem_impostos + valor_cbs) / (1 - icms)

    preco_novo = final9(preco_calculado)
    preco_sem_recomposicao = preco_novo

    ajuste_margem = False
    if csi and preco and sem_impostos >= csi:
        alvo = (sem_impostos - csi) / preco
        margem_sugerida = (preco_novo * (1 - icms) - valor_cbs - csi) / preco_novo
        # valores exatos, não arredondados: margem -0,0007% aparece como 0,00% mas é negativa
        if margem_sugerida < alvo and 1 - icms - alvo > 0:
            preco_novo = final9((valor_cbs + csi) / (1 - icms - alvo))
            ajuste_margem = True

    mercs = [_txt(linha.get(f"mercadologico{n}")) for n in range(1, 5)]
    merc = [".".join(mercs[:n]) for n in range(1, 5) if all(mercs[:n])]

    return {
        "id_loja": _txt(linha.get("id_loja")),
        "id_produto": _txt(linha.get("id_produto")),
        "descricao": _txt(linha.get("descricaocompleta")),
        "ncm": _txt(linha.get("ncm")),
        "merc": merc,
        "preco_atual": _r(preco),
        "aliq_pis": _r(pis * CEM, QUATRO_CASAS),
        "aliq_cofins": _r(cofins * CEM, QUATRO_CASAS),
        "aliq_icms": _r(icms * CEM, QUATRO_CASAS),
        "sem_icms": not _txt(linha.get("cst_icms")),
        "cclasstrib": _txt(linha.get("cclasstrib")),
        "cst_ibscbs": _txt(linha.get("cst_ibscbs")),
        "reducao_ibscbs": _r(reducao * CEM),
        "origem_cclass": _txt(linha.get("origem_cclass")),
        "aliq_cbs": _r(aliq_cbs * CEM, QUATRO_CASAS),
        "custo_com_imposto": _r(cci, QUATRO_CASAS),
        "custo_sem_imposto": _r(csi, QUATRO_CASAS),
        "preco_calculado": _r(preco_calculado),
        "ajuste_margem": ajuste_margem,
        "preco_sem_recomposicao": _r(preco_sem_recomposicao),
        "preco_sugerido": _r(preco_novo),
    }


def analise_smartpricing(linhas: list[dict]) -> dict:
    """linhas: reforma_smartpricing_base (1 por produto ativo × loja)."""
    itens = [simular(l) for l in linhas]

    lojas: dict[str, str] = {}
    descricao_merc: dict[str, str] = {}
    for l in linhas:
        lojas.setdefault(_txt(l.get("id_loja")), _txt(l.get("loja")))
        mercs = [_txt(l.get(f"mercadologico{n}")) for n in range(1, 5)]
        descs = [_txt(l.get(f"merc{n}")) for n in range(1, 5)]
        for n in range(1, 5):
            if all(mercs[:n]) and descs[n - 1]:
                descricao_merc[".".join(mercs[:n])] = descs[n - 1]

    return {
        "aliq_cbs_cheia": float(ALIQ_CBS * CEM),
        "lojas": [{"id": i, "nome": n} for i, n in sorted(lojas.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0)],
        "mercadologico": descricao_merc,
        "total": len(itens),
        "com_trava_margem": sum(1 for i in itens if i["ajuste_margem"]),
        "sem_icms": sum(1 for i in itens if i["sem_icms"]),
        "sem_custo": sum(1 for i in itens if not i["custo_com_imposto"]),
        "itens": itens,
    }
