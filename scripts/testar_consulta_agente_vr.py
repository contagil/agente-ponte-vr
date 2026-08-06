"""
Dispara UMA consulta no Agente VR e mostra o que voltou.

Serve para provar o caminho inteiro — Reforma Legal → cérebro → relay → agente
→ banco do cliente → volta — sem depender da tela nem de ter as 28 tarefas
cadastradas. É também a ferramenta de diagnóstico quando a sincronização falha
e você precisa saber em que ponto.

    .\\venv\\Scripts\\python.exe scripts\\testar_consulta_agente_vr.py
    .\\venv\\Scripts\\python.exe scripts\\testar_consulta_agente_vr.py reforma_ncm_cadastrado vr-copia-local

Sem argumentos, só checa a chave e mostra o escopo concedido (/whoami).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.core import agente_vr_client as agente  # noqa: E402


async def main():
    if not agente.integracao_configurada():
        print("AGENTE_VR_API_KEY ausente no .env — a integração não está configurada.")
        return 1

    print(f"cérebro: {agente.API_URL}")
    try:
        info = await agente.whoami()
    except agente.AgenteVRError as exc:
        print(f"[{exc.status_code}] {exc.mensagem}")
        return 1

    print(f"aplicação: {info.get('name')} ({info.get('app_id')})")
    escopo = info.get("scopes", [])
    print(f"escopo: {len(escopo)} tarefa(s) — {', '.join(escopo) or 'nenhuma'}")
    print(f"callback configurado: {info.get('callback_configurado')}")

    if len(sys.argv) < 3:
        print("\nPara disparar uma consulta de verdade:")
        print("  ...\\testar_consulta_agente_vr.py <task_id> <client_id>")
        return 0

    task_id, client_id = sys.argv[1], sys.argv[2]
    print(f"\ndisparando '{task_id}' no cliente '{client_id}'...")
    try:
        r = await agente.consultar(task_id, client_id)
    except agente.AgenteVRError as exc:
        print(f"[{exc.status_code}] {exc.mensagem}")
        # os códigos têm significados distintos e cada um pede uma ação
        pistas = {
            503: "agente offline — confira o serviço na máquina do cliente",
            504: "o agente não respondeu a tempo — tarefa pesada ou banco lento",
            409: "mais de um agente online para esse cliente — informe o agent_id",
            429: "limite de 2 consultas simultâneas por agente — tente em instantes",
            403: "a tarefa não está no escopo desta chave",
            404: "tarefa ou cliente não existe no cérebro",
        }
        if exc.status_code in pistas:
            print(f"  → {pistas[exc.status_code]}")
        return 1

    print(f"\nOK — {r.total} linha(s), executado em {r.executado_em}")
    print(f"agente: {r.agent_id} · cliente: {r.client_id} · consulta: {r.consulta_id}")
    print(f"colunas: {', '.join(r.columns)}")
    for linha in r.rows[:5]:
        print("  ", " | ".join(str(c)[:28] for c in linha))
    if r.total > 5:
        print(f"   ... mais {r.total - 5} linha(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
