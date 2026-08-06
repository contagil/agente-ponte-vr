"""
Imprime o SQL de uma tarefa do Agente VR, para colar no formulário do console.

Sem argumento, lista as 28 tarefas com os valores sugeridos de max_rows e
timeout_ms. Com o task_id, imprime só o SQL — pronto para `| clip`.

    .\\venv\\Scripts\\python.exe scripts\\sql_tarefa.py
    .\\venv\\Scripts\\python.exe scripts\\sql_tarefa.py reforma_ncm_cadastrado | clip

A fonte é sempre docs/agente-vr-catalogo-tarefas.md — o mesmo texto que o
validador roda contra o banco, então não há cópia para sair de sincronia.
"""
import importlib.util
import os
import sys

_AQUI = os.path.dirname(os.path.abspath(__file__))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# max_rows por tarefa (ver a tabela de volumes medidos no documento); o que não
# está aqui usa 10.000, que é folgado para consultas de cadastro.
MAX_ROWS = {
    "reforma_vinculo_produtos": 500_000,
    "reforma_ncm_produtos": 500_000,
    "reforma_ncm_cadastrado": 50_000,
    "reforma_municipios": 50_000,
    "reforma_ibs_municipal": 50_000,
    "reforma_cfoptiposaida": 50_000,
}
MAX_ROWS_PADRAO = 10_000
TIMEOUT_MS = 120_000


def _carregar_parser():
    spec = importlib.util.spec_from_file_location(
        "validador", os.path.join(_AQUI, "validar_tarefas_agente_vr.py"))
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def main():
    tarefas = _carregar_parser().extrair_sql_do_doc()

    if len(sys.argv) < 2:
        print(f"{len(tarefas)} tarefas — timeout_ms {TIMEOUT_MS:,} em todas\n".replace(",", "."))
        print(f"{'task_id':38} {'max_rows':>10}")
        print("-" * 50)
        for task_id in tarefas:
            linhas = MAX_ROWS.get(task_id, MAX_ROWS_PADRAO)
            print(f"{task_id:38} {linhas:>10,}".replace(",", "."))
        print("\nPara copiar o SQL de uma delas:")
        print("  .\\venv\\Scripts\\python.exe scripts\\sql_tarefa.py <task_id> | clip")
        return 0

    task_id = sys.argv[1]
    if task_id not in tarefas:
        print(f"tarefa desconhecida: {task_id}", file=sys.stderr)
        print(f"use uma de: {', '.join(tarefas)}", file=sys.stderr)
        return 1

    # sem quebra de linha extra no fim: vai direto para a área de transferência
    sys.stdout.write(tarefas[task_id])
    return 0


if __name__ == "__main__":
    sys.exit(main())
