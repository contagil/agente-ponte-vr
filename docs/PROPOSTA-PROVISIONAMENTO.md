# Proposta: provisionamento de cliente/agente pela API externa

> Rascunho técnico levado ao Danillo. Implementado como PROTÓTIPO na branch
> `draft/api-provisionamento-rl` do próprio agent-vr (não na main — sem aval
> do time ainda) pra viabilizar a revisão com código real, não só desenho.
> Espelha o formato de `PROPOSTA-API-EXTERNA.md`/`ACESSO-API-APP-EXTERNA.md`
> do próprio agent-vr.
>
> **Revisão do Danillo (04/08):** desenho aprovado em linha, com 3 ajustes —
> já aplicados no rascunho e refletidos abaixo: permissão por verbo em vez de
> uma flag única (§3.1), escopo por cliente (§3.1), e reuso da montagem do
> `agent.conf`/README que já existia em `admin_agent_package` (§3.2).

## 1. Objetivo

Hoje, cadastrar um cliente novo no Agente VR exige um humano logado no console
(AD + RBAC) fazendo três coisas manuais: criar o cliente, criar o agente,
baixar o pacote `.zip`. A intenção é que o Reforma Legal faça isso sozinho —
o cliente final nunca vê o console do agent-vr, e o TI da Contágil não
precisa ser acionado pra cada onboarding.

## 2. Por que não dá pra fazer com o que já existe

A ponte já tem uma chave de API (`AGENTE_VR_API_KEY`) e consome
`POST /api/v1/consultas` normalmente. Mas os três passos acima são
`/admin/clients`, `/admin/agents` e `/admin/agents/{id}/package` — protegidos
por `require_perm("clients.manage")`/`require_perm("agents.manage")`, que
dependem de **sessão de cookie de login AD** (`request.session.get("user")`
em `main.py::current_identity`), não de chave de API. É uma porta de
autenticação inteiramente diferente da que a ponte atravessa hoje — não é
questão de escopo, é questão de mecanismo.

As alternativas de contornar isso sem mexer no agent-vr foram descartadas:

- **Basic auth com `ADMIN_PASSWORD`** — é quebra-vidro (auth.py:
  "pra não perder o console se o AD cair"); usar em automação rotineira
  esgota o propósito da credencial de emergência e some a atribuição
  (toda ação aparece como `admin` na auditoria, sem saber qual cliente do
  RL pediu).
- **Conta de serviço no AD** com `clients.manage`+`agents.manage`, a ponte
  fazendo bind LDAPS e mantendo sessão de cookie — funciona sem mexer em
  código, mas tem o mesmo problema de atribuição, quebra silenciosamente se
  a senha rotacionar, e força uma superfície pensada pra humano a virar API.
  É o mesmo padrão que `ACESSO-API-APP-EXTERNA.md` rejeitou pro caso de
  leitura (apêndice "por que não um túnel") — aqui o raciocínio é idêntico.

A saída limpa é estender a API de chave (`/api/v1/*`) com um escopo novo,
específico pra provisionamento, sem tocar na autenticação de console.

## 3. Desenho proposto

### 3.1 Permissões por verbo + escopo por cliente, separado do escopo de leitura

Hoje `api_apps.scopes_json` é uma lista de `task_id` que a app pode consultar.
Provisionar cliente/agente é uma capacidade de natureza diferente (cria
identidade, emite token de enrollment) — não deveria compartilhar a mesma
lista, e (revisão do Danillo) não deveria ser uma flag única: no console essas
ações já são permissões separadas por peso — `clients.edit` (criar cliente),
`agents.create`, `agents.package` (o zip carrega o token de enrollment) e
`agents.revoke` (destrutiva). A API segue o mesmo corte, senão quem só precisa
provisionar ganha o poder de revogar junto:

```sql
ALTER TABLE api_apps ADD COLUMN provisionamento_json TEXT NOT NULL DEFAULT '[]';
-- lista de verbos concedidos, ex.: ["clients.edit","agents.create","agents.package","agents.revoke"]
```

**Escopo por cliente (2º ajuste):** a leitura já é limitada por `scopes_json`,
mas provisionar não tinha equivalente — com a permissão ligada, a app podia
mexer em qualquer cliente da frota, inclusive de outra integração. Corrigido
com uma carteira de clientes por app, com **herança automática**: o
`client_id` que a própria app cria entra na carteira dela na hora — ela nunca
precisa ser configurada manualmente pra operar sobre o que ela mesma criou,
mas também nunca enxerga cliente de fora dessa carteira:

```sql
ALTER TABLE api_apps ADD COLUMN provisionamento_clientes_json TEXT NOT NULL DEFAULT '[]';
```

**Recomendação (mantida):** uma chave de API **separada** só pra isso, não a
mesma `AGENTE_VR_API_KEY` de consultas — provisionar tem raio de explosão bem
maior (cria agente, emite token de uso único) que ler dado já pronto. Se a
chave de provisionamento vazar, o dano fica contido à carteira de clientes
dessa app, não à frota toda.

### 3.2 Endpoints (mesma auth de `api_external.py`, escopo diferente)

```
POST /api/v1/provisionamento/clientes
  Authorization: Bearer <chave-de-provisionamento>
  {"client_id": "mercado-silva", "name": "Mercado Silva Ltda", "notes": "..."}

  -> 201 {"status": "created"}
  -> 409 client_id já existe
  -> 422 client_id fora do padrão (a-z, 0-9, hífen)
  -> 403 chave sem pode_provisionar
```

```
POST /api/v1/provisionamento/agentes
  {"agent_id": "agent-mercado-silva-01", "client_id": "mercado-silva", "tier": "test"}

  -> 201 {"agent_id": "...", "enroll_token": "..."}
  -> 404 client_id não existe
  -> 409 agent_id já existe
  -> 422 tier fora de test/prod
```
Mesma regra de hoje: o `enroll_token` só é exibido nesta resposta — depois
disso só existe embutido no pacote.

```
GET /api/v1/provisionamento/agentes/{agent_id}/pacote
  -> 200 application/zip (agent-vr.exe + agent.conf + LEIA-ME.txt)
  -> 404 agente não existe
  -> 409 token já consumido (agente já instalado — pra reinstalar, revogar e criar de novo)
```

```
DELETE /api/v1/provisionamento/agentes/{agent_id}
  -> 200 {"status": "revoked"}
```
Necessário pro fluxo de "perdi o zip" / "preciso reinstalar" — sem isso, um
zip perdido significa um `agent_id` morto pra sempre (token de uso único, sem
como reemitir pro mesmo agente).

Implementação, do lado do agent-vr: cada um desses é essencialmente o mesmo
corpo de `admin_client_create`/`admin_agent_create`/`admin_agent_package`/
`admin_agent_delete` que já existe em `main.py`, só trocando a dependency de
`require_perm(...)` por uma nova `require_provisionamento(perm: str)` que
checa o verbo na `provisionamento_json` da app e o `client_id` alvo contra a
`provisionamento_clientes_json` dela — mesmo padrão de `require_scope` em
`api_external.py`, não uma rota nova do zero.

**Terceiro ajuste do Danillo:** o `agent.conf`/LEIA-ME.txt que o endpoint de
pacote monta é o MESMO texto que `admin_agent_package` já gerava no console —
estava duplicado no rascunho inicial. Extraído pra uma função única
(`montar_pacote_agente`, em `main.py`), chamada pelos dois pontos de entrada —
sem isso, a próxima mudança nesse fluxo (mexemos nele há pouco por causa da
detecção automática do `vr.properties`) divergiria os dois sem ninguém notar.

### 3.3 Auditoria

Cada chamada grava em `db.audit(...)` com `app_id` no lugar de `sam` — dá pra
diferenciar "criado pelo Fulano no console" de "criado via API pela aplicação
reforma-legal", coisa que as alternativas descartadas (§2) não conseguiam.

### 3.4 Configuração de banco pela API — RASCUNHO SEPARADO, mais sensível (07/08)

Implementado na branch `draft/api-db-config-rl` (não `draft/api-provisionamento-rl`
— é um rascunho novo, ainda sem revisão do Danillo). Fecha a lacuna do §4
original: o Reforma Legal passa a poder configurar o `dsn=` do cliente sem
precisar de alguém editando o `agent.conf` na máquina dele.

**Por que é uma branch/decisão separada da §3.1-3.3:** aqueles endpoints só
criam identidade (cliente/agente) e movem bytes que já existiam (o zip). Este
aqui faz a senha do Postgres do CLIENTE passar pela aplicação consumidora (o
Reforma Legal, via a ponte) antes de chegar cifrada no agente — hoje isso só
acontecia dentro do console (`admin_db_config`, sessão AD). Raio de explosão
bem maior, pede permissão própria.

```
GET /api/v1/provisionamento/agentes/{agent_id}/status
  -> 200 {"agent_id", "enrolled": bool, "online": bool, "db_status"}
```
Necessário porque o agente só pode ser configurado depois de ter feito
enrollment ao menos uma vez (é a chave X25519 dele que cifra a senha — sem
isso não tem pra quem cifrar). O consumidor usa isto pra saber quando liberar
a etapa "configurar banco" na tela dele — antes disso, só existe a opção
manual (editar o `agent.conf`).

```
POST /api/v1/provisionamento/agentes/{agent_id}/db-config
  {"host": "...", "port": "...", "user": "...", "password": "...", "dbname": "..."}

  -> 202 {"request_id": "...", "status": "dispatched" | "queued (agent offline)"}
  -> 404 agente não existe ou ainda não fez enrollment
  -> 422 host/port/user/dbname faltando
```
Mesmo corpo de `admin_db_config` (`main.py`) — a senha é cifrada pro X25519 do
agente (`sealing.seal_for_agent`) ANTES de qualquer persistência local; nunca
grava em claro, nunca vai pro `audit` (só host/user/dbname, igual ao console).
O agente só aplica depois de CONECTAR de verdade — a resposta aqui é sempre
"pedido aceito", não "banco configurado".

```
GET /api/v1/provisionamento/agentes/{agent_id}/db-config/{request_id}
  -> 200 {"status": "pending" | "dispatched" | "ok" | "error", "error": "..."}
```
Pra quem chamou saber o desfecho — o `dispatch` acima só confirma que o
pedido foi aceito, não que o agente conseguiu conectar.

**Permissão:** verbo novo `agents.dbconfig`, mesmo nome já usado no RBAC do
console (`require_perm("agents.dbconfig")` em `admin_db_config`) — segue o
mesmo corte da §3.1. Testado com `TestClient` (11 cenários, incluindo a
checagem de que a senha nunca aparece em claro no `config_enc` persistido, e
o `403` de escopo por cliente).

## 4. O que ainda fica de fora, mesmo com isso implementado

- ~~**O `dsn=` do banco do cliente.**~~ Resolvido pela §3.4 acima (rascunho
  separado `draft/api-db-config-rl`, pendente de revisão). O pacote continua
  saindo com o placeholder (`postgresql://usuario:senha@127.0.0.1:5432/vr`) —
  quem instala ainda pode preenchê-lo manualmente ou confiar no
  `C:\vr\vr.properties`, mas agora existe um terceiro caminho: o RL manda a
  senha pela API depois que o agente faz enrollment. Fica explícito de novo:
  isso significa que o Reforma Legal passa a manusear, mesmo que de
  passagem, a credencial do Postgres do cliente — é exatamente o motivo de
  ser um rascunho à parte, não incluído automaticamente no aval da §3.
- **Quem "dona" o cadastro depois de criado.** O cliente/agente criado pela
  API continua aparecendo no console pra gestão manual (revogar, trocar
  tier)? Presumo que sim (mesma tabela), mas vale confirmar com o Danillo.

## 5. Como isso se encaixa na ponte

Sem mudança de arquitetura: a ponte já faz long-poll no RL e disca pro
cérebro. Só ganha mais um tipo de pedido na fila (`tipo: "provisionar"` em vez
de `tipo: "sincronizar"`), chamando os endpoints novos em vez de
`/api/v1/consultas`. O resultado entregue de volta pro RL muda de "JSON de
análise" pra "bytes do zip" — o único ponto de desenho novo do nosso lado é
como devolver um binário pela fila (base64 no JSON de resultado é o caminho
mais simples, dado o volume pequeno do pacote — ~poucos MB).

## 6. Decisões em aberto (levar ao Danillo)

- [ ] Chave separada pra provisionamento, ou o mesmo `AGENTE_VR_API_KEY` com
      escopo ampliado?
- [ ] Vale incluir `db-config` na mesma proposta, ou fica pra depois?
- [ ] `DELETE /provisionamento/agentes/{id}` — necessário desde o início, ou
      entra só quando o primeiro caso real de reinstalação aparecer?
- [ ] Algum limite de quantos clientes/agentes o Reforma Legal pode criar
      por período (evitar abuso/erro em cascata gerando dezenas de agentes)?
