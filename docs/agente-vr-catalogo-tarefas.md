# Tarefas do catálogo do Agente VR usadas pelo Reforma Legal

O Reforma Legal consome a **API de leitura externa** do cérebro do `agent-vr`
(`docs/PROPOSTA-API-EXTERNA.md` daquele projeto). A API **nunca aceita SQL**: ela
só invoca `task_id` que já existem no catálogo, cadastrados por humano logado no
console do agent-vr (`Catálogo → Nova tarefa`).

Ou seja: **antes de a tela do Agente VR funcionar, alguém precisa cadastrar as
tarefas abaixo no console e colocá-las no escopo da chave de API do Reforma
Legal.** Enquanto isso não acontece, `GET /api/agente-vr/status` devolve
`tarefas_faltando_no_escopo` preenchido e a tela avisa.

Todas são `SELECT` puro, sem parâmetro, no banco do VR (o agente abre a sessão
em modo leitura). Os `task_id` são os defaults do backend — se cadastrarem com
outro nome, aponte a env var `AGENTE_VR_TASK_<CHAVE>` correspondente.

**Obrigatórias** (núcleo tributário — sem elas a sincronização falha):

| task_id | chave (env) | O que traz |
|---|---|---|
| `reforma_ncm_cadastrado` | `NCM_CADASTRADO` | NCMs nível 3 cadastrados |
| `reforma_ncm_produtos` | `NCM_PRODUTOS` | Produtos ativos com NCM |
| `reforma_ncm_ativos` | `NCM_ATIVOS` | NCMs distintos do sortimento ativo |
| `reforma_cst_cadastrado` | `CST` | CSTs cadastrados + indicadores |
| `reforma_cclasstrib_cadastrada` | `CCLASSTRIB` | CClassTrib + CST vinculado |
| `reforma_cclasstrib_vinculo_ncm` | `CCLASSTRIB_NCM` | Qtd. de NCM por CClassTrib |
| `reforma_cclasstrib_vinculo_produto` | `CCLASSTRIB_PRODUTO` | Qtd. de produto por CClassTrib |

**Complementares** (cada grupo que falhar vira "análise indisponível"; o resto
do relatório sai normalmente):

| task_id | chave (env) | Análise |
|---|---|---|
| `reforma_estados` | `ESTADOS` | 4/5/6 — UF/Município/CBS |
| `reforma_ibs_estadual` | `IBS_ESTADUAL` | 4/5/6 |
| `reforma_ibs_municipal` | `IBS_MUNICIPAL` | 4/5/6 |
| `reforma_municipios` | `MUNICIPIOS` | 4/5/6 |
| `reforma_cbs` | `CBS` | 4/5/6 e 9 |
| `reforma_vinculo_produtos` | `VINCULO_PRODUTOS` | 7 — vínculo produto/NCM |
| `reforma_vinculo_produto` | `VINCULO_PRODUTO` | 7 |
| `reforma_vinculo_ncm` | `VINCULO_NCM` | 7 |
| `reforma_classificacoes` | `CLASSIFICACOES` | 7 |
| `reforma_tipodebitocredito` | `TIPODEBITOCREDITO` | 8 e 11 |
| `reforma_debitocredito` | `DEBITOCREDITO` | 8 e 11 |
| `reforma_tiposaida` | `TIPOSAIDA` | 8 e 11 |
| `reforma_cfoptiposaida` | `CFOPTIPOSAIDA` | 8 e 11 |
| `reforma_movimento_tiposaida` | `MOVIMENTO_TIPOSAIDA` | 11 (movimento real) |
| `reforma_operacoes_sem_tiposaida` | `OPERACOES_SEM_TIPOSAIDA` | 11 (movimento real) |
| `reforma_parametro_nfe` | `PARAMETRO_NFE` | 9 — parâmetro de data |
| `reforma_parametro_pdv` | `PARAMETRO_PDV` | 9 |
| `reforma_tipoautor` | `TIPOAUTOR` | 10 — eventos |
| `reforma_tipoevento` | `TIPOEVENTO` | 10 |
| `reforma_eventos_totais` | `EVENTOS_TOTAIS` | 10 |
| `reforma_empresas` | `EMPRESAS` | Identificação das lojas |

> **Os nomes das colunas importam.** O backend lê cada linha pelo nome da coluna
> (em minúsculas). Mantenha os aliases exatamente como estão abaixo.

O SQL é o mesmo do app desktop de auditoria
(`configuracao-vr-leitura-reforma-tributaria/analise_reforma.py`), só com os
aliases explicitados.

---

## `reforma_ncm_cadastrado`

```sql
SELECT id, ncm1, ncm2, ncm3, descricao, id_situacaocadastro, datainicio, datatermino
FROM public.ncm
WHERE nivel = 3;
```

## `reforma_ncm_produtos`

Um produto pode aparecer em mais de uma loja em `produtocomplemento` — daí o
`DISTINCT`. É a consulta mais pesada das sete (sugestão: `timeout_ms` 120000).

```sql
SELECT DISTINCT
       p.id AS id_produto,
       to_char(p.ncm1, 'FM0000') || to_char(COALESCE(p.ncm2, 0), 'FM00')
         || to_char(COALESCE(p.ncm3, 0), 'FM00') AS ncm,
       pat.codigobarras AS ean,
       p.descricaocompleta AS descricao
FROM public.produto p
JOIN public.produtocomplemento pc
  ON pc.id_produto = p.id AND pc.id_situacaocadastro = 1
LEFT JOIN public.produtoautomacao pat ON pat.id_produto = p.id
WHERE p.ncm1 IS NOT NULL;
```

## `reforma_ncm_ativos`

```sql
SELECT DISTINCT p.ncm1, p.ncm2, p.ncm3
FROM public.produto p
JOIN public.produtocomplemento pc
  ON pc.id_produto = p.id AND pc.id_situacaocadastro = 1
WHERE p.ncm1 IS NOT NULL;
```

## `reforma_cst_cadastrado`

```sql
SELECT cst, descricao, datainicio, datatermino, id_situacaocadastro,
       grupoibscbs, grupoibscbsmono, gruporeducao, grupodiferimento
FROM reformatributaria.cst
ORDER BY cst;
```

## `reforma_cclasstrib_cadastrada`

`grupoibscbs` aqui é o do **CST vinculado** (é ele que diz se a operação é de
tributação) — usado na regra de Alíquota Zero. O `ct.id` e o
`ct.id_situacaocadastro` são usados pela análise 11, que precisa localizar as
CClassTrib alvo (410002, 410001/410003/410026) pelo id interno.

```sql
SELECT ct.id,
       ct.cclasstrib,
       ct.descricao   AS descricao,
       c.cst          AS cst,
       ct.aliquotazero,
       ct.reducao,
       ct.id_situacaocadastro,
       c.grupoibscbs
FROM reformatributaria.classificacaotributaria ct
JOIN reformatributaria.cst c ON c.id = ct.id_cstibscbs
ORDER BY ct.cclasstrib;
```

## `reforma_cclasstrib_vinculo_ncm`

```sql
SELECT ct.cclasstrib, count(*) AS qtd
FROM reformatributaria.classificacaotributariancm cn
JOIN reformatributaria.classificacaotributaria ct ON ct.id = cn.id_classificacao
GROUP BY ct.cclasstrib;
```

## `reforma_cclasstrib_vinculo_produto`

```sql
SELECT ct.cclasstrib, count(*) AS qtd
FROM reformatributaria.classificacaotributariaproduto cp
JOIN reformatributaria.classificacaotributaria ct ON ct.id = cp.id_classificacao
GROUP BY ct.cclasstrib;
```

---

# Complementares

## Análise 4/5/6 — UF, Município e CBS

A checagem de sobreposição de períodos e de "UF parcialmente cadastrada" é feita
no backend, sobre estas linhas — por isso as consultas trazem os cadastros, não
um veredito pronto.

### `reforma_estados`

```sql
SELECT e.id, e.sigla, e.descricao
FROM public.estado e
WHERE e.sigla <> 'EX'
ORDER BY e.sigla;
```

### `reforma_ibs_estadual`

```sql
SELECT ie.id, ie.id_estado, ie.porcentagem, ie.datainicio, ie.datatermino
FROM reformatributaria.ibsestadual ie
WHERE ie.id_situacaocadastro = 1;
```

### `reforma_ibs_municipal`

```sql
SELECT im.id, im.id_municipio, m.descricao AS municipio, e.sigla,
       im.porcentagem, im.datainicio, im.datatermino
FROM reformatributaria.ibsmunicipal im
JOIN public.municipio m ON m.id = im.id_municipio
JOIN public.estado e ON e.id = im.id_estado
WHERE im.id_situacaocadastro = 1
ORDER BY e.sigla, m.descricao;
```

### `reforma_municipios`

Todos os municípios (~5.500 linhas) com a marca de já ter cadastro de IBS
municipal — é o que distingue "UF ainda não configurada" de "UF configurada
pela metade".

```sql
SELECT e.sigla, m.id AS id_municipio, m.descricao AS municipio,
       EXISTS(
           SELECT 1 FROM reformatributaria.ibsmunicipal im
           WHERE im.id_municipio = m.id AND im.id_situacaocadastro = 1
       ) AS cadastrado
FROM public.municipio m
JOIN public.estado e ON e.id = m.id_estado
WHERE e.sigla <> 'EX'
ORDER BY e.sigla, m.descricao;
```

### `reforma_cbs`

Traz também os excluídos (`id_situacaocadastro <> 1`) — o status distingue.

```sql
SELECT id, porcentagem, datainicio, datatermino, id_situacaocadastro
FROM public.cbs
ORDER BY datainicio;
```

## Análise 7 — vínculo produto/NCM

⚠️ `reforma_vinculo_produtos` é a consulta mais volumosa de todas (um registro
por produto ativo × loja). Ajuste o `max_rows` da tarefa ao porte do cliente; o
backend guarda contagens completas e só amostras de 200 linhas no relatório.

### `reforma_vinculo_produtos`

```sql
SELECT p.id AS id_produto, p.ncm1, p.ncm2, p.ncm3,
       pat.codigobarras AS ean, pc.id_loja,
       p.descricaocompleta AS descricao
FROM public.produto p
JOIN public.produtocomplemento pc
  ON pc.id_produto = p.id AND pc.id_situacaocadastro = 1
LEFT JOIN public.produtoautomacao pat ON pat.id_produto = p.id
WHERE p.ncm1 IS NOT NULL;
```

### `reforma_vinculo_produto`

```sql
SELECT id_produto, id_loja, id_classificacao
FROM reformatributaria.classificacaotributariaproduto;
```

### `reforma_vinculo_ncm`

```sql
SELECT ncm1, ncm2, ncm3, id_loja, id_classificacao
FROM reformatributaria.classificacaotributariancm;
```

### `reforma_classificacoes`

```sql
SELECT ct.id, ct.cclasstrib, ct.descricao, c.cst
FROM reformatributaria.classificacaotributaria ct
JOIN reformatributaria.cst c ON c.id = ct.id_cstibscbs;
```

## Análises 8 e 11 — débito/crédito e Tipo de Saída

### `reforma_tipodebitocredito`

```sql
SELECT id, sigla, descricao
FROM reformatributaria.tipodebitocredito;
```

### `reforma_debitocredito`

```sql
SELECT dc.id, dc.cod_xml, dc.descricao, dc.id_situacaocadastro,
       td.sigla, td.descricao AS tipo_descricao
FROM reformatributaria.debitocredito dc
JOIN reformatributaria.tipodebitocredito td ON td.id = dc.id_tipodebitocredito
ORDER BY td.sigla, dc.cod_xml;
```

### `reforma_tiposaida`

```sql
SELECT id, descricao, id_situacaocadastro,
       id_debitocredito, id_classificacaotributaria
FROM public.tiposaida;
```

### `reforma_cfoptiposaida`

**Não troque o `to_jsonb(cts)` por colunas nomeadas.** Algumas instalações do VR
têm `id_debitocredito` / `id_classificacaotributaria` em `cfoptiposaida`
(vínculo sobrescrito por CFOP) e outras não — citar a coluna direto quebraria a
tarefa nos bancos onde ela não existe. O backend lê o JSON e usa o campo quando
ele está presente.

**O `::text` no final não é opcional.** Sem ele, a coluna vai como `jsonb`; o
driver Go do agente decodifica `jsonb` para `map[string]interface{}` antes de
escrever o CSV, e a formatação padrão do Go produz `map[cfop:5.409 id:200 ...]`
— não é JSON válido, e `json.loads()` no backend falha e devolve vazio em
silêncio (achado real em 04/08/2026, confirmado contra o `vr_teste`). Com
`::text`, o Postgres já entrega a string `{"cfop":"5.409","id":200,...}` pronta,
e o driver trata como texto comum.

```sql
SELECT cts.id_tiposaida,
       cts.cfop,
       c.descricao AS descricao_cfop,
       to_jsonb(cts)::text AS linha
FROM public.cfoptiposaida cts
LEFT JOIN public.cfop c
  ON regexp_replace(c.cfop, '[^0-9]', '', 'g') = regexp_replace(cts.cfop, '[^0-9]', '', 'g');
```

### `reforma_movimento_tiposaida`

Movimento real na escrituração fiscal (`public.escritaitem` é particionada por
mês; consultar a tabela-mãe cobre o histórico inteiro). A lista de CFOP é a
regra de negócio das três checagens da análise 11 e está fixa no SQL de
propósito — a API externa não aceita parâmetro vindo do RL para isso.

```sql
SELECT id_tiposaida,
       regexp_replace(cfop, '[^0-9]', '', 'g') AS cfop,
       count(*) AS qtd
FROM public.escritaitem
WHERE regexp_replace(cfop, '[^0-9]', '', 'g') IN
      ('5927','6927','5151','6151','5152','6152','5409','6409','5910','6910')
  AND id_tiposaida IS NOT NULL
GROUP BY 1, 2;
```

### `reforma_operacoes_sem_tiposaida`

Lançamento com CFOP do grupo e **sem nenhum** Tipo de Saída vinculado — erro
que só o cruzamento com o movimento enxerga.

```sql
SELECT regexp_replace(cfop, '[^0-9]', '', 'g') AS cfop, count(*) AS qtd
FROM public.escritaitem
WHERE regexp_replace(cfop, '[^0-9]', '', 'g') IN
      ('5927','6927','5151','6151','5152','6152','5409','6409','5910','6910')
  AND id_tiposaida IS NULL
GROUP BY 1;
```

## Análise 9 — parâmetro "Data envio IBS/CBS"

O `id_parametro` varia de instalação para instalação, então o parâmetro é
localizado pela **descrição**, dentro da própria consulta. Se algum cliente
tiver a descrição redigida de outro jeito, ajuste o `ILIKE`.

### `reforma_parametro_nfe`

```sql
SELECT l.id AS id_loja, l.descricao AS loja, pv.valor,
       (SELECT p.descricao FROM public.parametro p
         WHERE p.descricao ILIKE '%DATA%ENVIO%TRIBUTOS%IBS%CBS%'
         ORDER BY p.id LIMIT 1) AS descricao_parametro
FROM public.loja l
LEFT JOIN public.parametrovalor pv
       ON pv.id_loja = l.id
      AND pv.id_parametro = (SELECT p.id FROM public.parametro p
                              WHERE p.descricao ILIKE '%DATA%ENVIO%TRIBUTOS%IBS%CBS%'
                              ORDER BY p.id LIMIT 1)
ORDER BY l.id;
```

### `reforma_parametro_pdv`

```sql
SELECT l.id AS id_loja, l.descricao AS loja, pv.valor,
       (SELECT p.descricao FROM pdv.parametro p
         WHERE p.descricao ILIKE '%DATA%ENVIO%IBS%CBS%NFC%'
         ORDER BY p.id LIMIT 1) AS descricao_parametro
FROM public.loja l
LEFT JOIN pdv.parametrovalor pv
       ON pv.id_loja = l.id
      AND pv.id_parametro = (SELECT p.id FROM pdv.parametro p
                              WHERE p.descricao ILIKE '%DATA%ENVIO%IBS%CBS%NFC%'
                              ORDER BY p.id LIMIT 1)
ORDER BY l.id;
```

## Análise 10 — eventos de IBS/CBS

### `reforma_tipoautor`

```sql
SELECT id, codigo, descricao
FROM reformatributaria.tipoautor
ORDER BY codigo;
```

### `reforma_tipoevento`

```sql
SELECT codigo, descricao, id_tipoautor
FROM reformatributaria.tipoevento
ORDER BY codigo;
```

### `reforma_eventos_totais`

```sql
SELECT (SELECT count(*) FROM reformatributaria.evento)     AS qtd_evento,
       (SELECT count(*) FROM reformatributaria.eventoitem) AS qtd_eventoitem;
```

## Identificação das lojas

### `reforma_empresas`

`public.empresa` **não** é a identidade fiscal (guarda um cadastro de
franquia/cobrança da própria VR). A razão social e o CNPJ reais de cada
loja/filial ficam em `public.fornecedor`, via `loja.id_fornecedor`.

```sql
SELECT l.descricao AS loja, f.razaosocial, f.cnpj
FROM public.loja l
JOIN public.fornecedor f ON f.id = l.id_fornecedor
WHERE l.id_situacaocadastro = 1 AND f.id_situacaocadastro = 1
ORDER BY l.id;
```

---

## Cadastro da aplicação consumidora

A chave de API do Reforma Legal é provisionada pelos endpoints
`/admin/api-apps` do cérebro (RBAC `access.manage`). A chave aparece **uma única
vez**, na resposta da criação — guardar no cofre e colocar em `AGENTE_VR_API_KEY`.

O escopo precisa conter os sete `task_id` acima. O RL usa só o modo síncrono
(§4.1), então `callback_url` não é necessário.

## `max_rows` e `timeout_ms` no cadastro

⚠️ **O teto real não é o do formulário do catálogo (500.000) — é o do
manifesto do core do agente, que rejeita a tarefa inteira se o `max_rows`
cadastrado ultrapassar o limite dele.** Descoberto em 06/08/2026, testando
`reforma_vinculo_produtos` de ponta a ponta pela primeira vez através do
agente (os testes anteriores validavam o SQL direto no Postgres, que não
passa por essa checagem): com `max_rows=500.000`, a tarefa falhou com

```
policy: job excede limites do manifesto (rows 500000>100000 ou timeout 60000>300000)
```

Ou seja, o manifesto deste agente/tier limita em **100.000 linhas** — bem
abaixo do teto de 500.000 que o formulário do catálogo permite digitar. Se
o grupo da tarefa não for obrigatório (é o caso de `vinculo`), o efeito
prático é só a análise correspondente sumir do relatório como "indisponível"
— silencioso o bastante pra passar despercebido numa primeira olhada.

**Cadastre `max_rows` ≤ 100.000 em todas as tarefas** até confirmar (com
quem administra o agent-vr) se esse teto vale pra frota toda ou só pra
agentes tier `test`. Volumes medidos em 31/07/2026 contra o `vr_teste`
(2 lojas, ~19,8 mil produtos ativos), com
`scripts/validar_tarefas_agente_vr.py --contar`:

| Consulta | Linhas medidas | `max_rows` sugerido | `timeout_ms` |
|---|---:|---:|---:|
| `reforma_vinculo_produtos` | 19.812 | **100.000** | 120.000 |
| `reforma_ncm_produtos` | 16.561 | **100.000** | 120.000 |
| `reforma_ncm_cadastrado` | 13.976 | 50.000 | 120.000 |
| `reforma_municipios` | 5.565 | 50.000 | 120.000 |
| `reforma_ibs_municipal` | 223 | 50.000 | 120.000 |
| `reforma_cfoptiposaida` | 179 | 50.000 | 120.000 |
| demais | < 1.000 | 10.000 | 120.000 |

`max_rows` é teto, não pré-alocação — cadastrar folgado não custa nada
*dentro do que o manifesto permite*, e estourar o teto **trunca o resultado
em silêncio** (a análise passa a subcontar sem avisar).

O ponto de atenção é `reforma_vinculo_produtos`, que cresce com
**produtos ativos × lojas**: um cliente com mais de ~30 mil produtos ativos
(considerando o número de lojas) passa dos 100.000 — e, diferente do teto do
catálogo, não tem como simplesmente cadastrar um número maior. Se isso
acontecer de verdade, o teto do manifesto precisa subir (falar com quem
administra o agent-vr), não só o campo do formulário.

Se algum cliente exigir `timeout_ms` acima de 165.000, lembre de subir junto o
`AGENTE_VR_TIMEOUT_S` do backend — ele precisa ser maior que o timeout da
tarefa + 15s de margem, senão o RL desiste antes de o cérebro responder.

## Limites que a tela precisa respeitar

- Máx. **2 consultas em voo por agente** (429 no excedente) — por isso o
  `/sincronizar` dispara as sete **em sequência**, não em paralelo.
- O síncrono espera até `timeout_ms` da tarefa + 15s; o cliente HTTP do RL usa
  `AGENTE_VR_TIMEOUT_S` (default 180s), que precisa ser maior que isso.
- Agente offline = 503; mais de um agente online sem `agent_id` = 409.
