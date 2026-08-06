# agente-vr-ponte

Processo que consome a API de leitura do **agent-vr** e entrega o resultado
tratado pro **Reforma Legal** — sem que o RL precise da `AGENTE_VR_API_KEY`,
do pacote da Calculadora RFB, nem de alcançar a rede interna da Contágil.

## Por que este projeto existe

O Reforma Legal é externo (hospedado fora da rede da Contágil). O cérebro do
agent-vr só existe em `10.0.100.204:8040`, rede interna, sem TLS — o RL não
alcança esse endereço de jeito nenhum, e não deveria guardar a chave de acesso
mesmo que alcançasse (ela dá leitura ao banco real de clientes).

A solução, seguindo `docs/ACESSO-API-APP-EXTERNA.md` do próprio agent-vr: um
componente rodando **dentro** da rede da Contágil, que nunca recebe conexão
de fora — só disca pra fora, dos dois lados:

```
 REDE DA CONTÁGIL                                    RL (externo)
┌───────────────────────────┐                      ┌──────────────────┐
│ cérebro         ponte     │                       │ reforma.legal    │
│ 10.0.100.204   (aqui)     │                       │                  │
│      ▲            │       │                       │                  │
│      └─ consulta ─┘       │                       │                  │
│      (sob demanda)        │                       │                  │
│                   │        │                       │                  │
│                   └────────┼──── long-poll ───────▶│ /ponte/proximo-  │
│                            │                       │  pedido          │
│                            │◀──── pedido ──────────│                  │
│                            │                       │                  │
│                            │──── POST resultado ──▶│ /ponte/resultado │
└───────────────────────────┘                       └──────────────────┘
```

Mesmo princípio do agente do agent-vr, um nível acima: quem está dentro
estabelece a conexão pra fora, nunca o contrário.

## Fluxo

1. Um usuário do RL clica em "Sincronizar agora". O backend do RL grava um
   **pedido** (`client_id`, `agent_id`, `cclasstrib_lista`) e devolve na hora
   — não faz nada além disso.
2. Esta ponte fica em `GET /agente-vr/ponte/proximo-pedido` (long-poll) até um
   pedido aparecer.
3. Reivindica o pedido, dispara as 28 consultas no cérebro (`app/core/
   agente_vr_client.py`), roda as 11 análises (`app/core/analise_vr.py`,
   cruzando com a base oficial da RFB via `app/core/rfb_reference.py`).
4. Entrega o resultado com `POST /agente-vr/ponte/resultado/{pedido_id}`.
5. O front do RL faz poll em `/agente-vr/analises/status/{pedido_id}` até ver
   `concluido` ou `erro`.

Funciona para **qualquer** cliente que qualquer usuário do RL configurar — o
`client_id`/`agent_id`/lista de CClassTrib vêm dentro do pedido, nada é fixo
no código.

## Rodando

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
# preencher AGENTE_VR_API_KEY, PONTE_API_KEY (mesma do .env do RL) e RL_BASE_URL
```

Coloque o pacote da Calculadora RFB em `data/calculadora_rfb/` (precisa
conter `codigo-fonte-backend.zip`) — a `Tabela_NCM_Vigente_*.json` já vem
neste repositório.

```powershell
.\venv\Scripts\python.exe -m app.main
```

Fica rodando (log em `INFO`), disparando o long-poll em loop e reportando um
heartbeat rico (escopo, disponibilidade da base RFB) a cada 60s por padrão.
`Ctrl+C` encerra.

## Onde isso deveria rodar de verdade

Hoje roda na sua máquina de desenvolvimento (que já está na rede da
Contágil). Para produção, precisa de uma VM interna de verdade — igual o
cérebro (`srvms09`) ou uma nova, dedicada. Ver `docs/ACESSO-API-APP-EXTERNA.md`
§3 no repo do agent-vr: provisionar a VM é responsabilidade da infra deles;
manter este processo rodando nela (systemd, reinício automático) é nossa.

## Segredos

- `AGENTE_VR_API_KEY` — só existe aqui. Nunca deve voltar a existir no RL.
- `PONTE_API_KEY` — compartilhada só com o RL (`AGENTE_VR_PONTE_API_KEY` no
  `.env` dele). Autentica os três endpoints `/ponte/*`, nada a ver com a
  chave acima.
- O pacote da Calculadora RFB e o `.env` ficam fora do git (`.gitignore`).

## Estrutura

| Arquivo | Papel |
|---|---|
| `app/main.py` | Loop principal — long-poll, heartbeat, processamento |
| `app/cliente_rl.py` | HTTP client pro RL (long-poll + entrega + heartbeat) |
| `app/sincronizacao.py` | As 28 tarefas, agrupadas, e a orquestração da sincronização |
| `app/core/agente_vr_client.py` | Cliente da API de leitura do cérebro |
| `app/core/rfb_reference.py` | Base oficial (zip da Calculadora RFB → SQLite) |
| `app/core/analise_vr.py` | As 11 análises tributárias |
| `docs/agente-vr-catalogo-tarefas.md` | SQL de cada uma das 28 tarefas do catálogo |
| `scripts/` | Ferramentas de diagnóstico (testar uma consulta, validar o catálogo contra um banco real) |
