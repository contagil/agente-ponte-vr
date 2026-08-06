"""
Base oficial de referência da Reforma Tributária (CST, CClassTrib, NCM aplicável,
percentuais de redução), montada a partir do pacote da Calculadora Offline da
Receita Federal.

Portado do projeto `configuracao-vr-leitura-reforma-tributaria` (rfb_reference.py).
Reaplica localmente, num SQLite descartável, as migrações Flyway que vêm dentro do
`codigo-fonte-backend.zip` — nenhum instalador da calculadora é executado, só os
scripts `.sql` são lidos.

Caminhos por env var (o pacote da RFB tem ~260MB e não entra no repositório):
- `RFB_CALCULADORA_DIR` — pasta que contém o `codigo-fonte-backend.zip`.
- `RFB_CACHE_SQLITE`    — arquivo de cache a gerar/reutilizar.
"""
import glob
import os
import re
import sqlite3
import threading
import zipfile

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CALCULADORA_DIR = os.getenv(
    "RFB_CALCULADORA_DIR", os.path.join(_BASE_DIR, "data", "calculadora_rfb")
)
CACHE_SQLITE = os.getenv(
    "RFB_CACHE_SQLITE", os.path.join(_BASE_DIR, "data", "rfb_reference_cache.sqlite")
)

# Montar a base leva minutos e grava um arquivo só; duas requisições simultâneas
# construindo o mesmo cache se atropelariam (uma apaga o .sqlite que a outra
# está escrevendo).
_lock = threading.Lock()


class BaseRFBIndisponivel(Exception):
    """Pacote da Calculadora RFB ausente ou ilegível neste servidor."""


def _extrair_backend(calculadora_dir: str, destino: str) -> str:
    if not os.path.isdir(calculadora_dir):
        raise BaseRFBIndisponivel(
            f"Pasta da Calculadora RFB não encontrada: {calculadora_dir}. "
            "Copie o pacote da Calculadora Offline da RFB para esse caminho ou "
            "aponte outro em RFB_CALCULADORA_DIR."
        )
    zip_path = None
    for nome in os.listdir(calculadora_dir):
        if nome.lower() == "codigo-fonte-backend.zip":
            zip_path = os.path.join(calculadora_dir, nome)
            break
    if not zip_path:
        raise BaseRFBIndisponivel(
            f"codigo-fonte-backend.zip não encontrado em {calculadora_dir}."
        )
    os.makedirs(destino, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(destino)
    return destino


def montar_base(calculadora_dir: str | None = None,
                cache_sqlite: str | None = None,
                forcar: bool = False) -> str:
    calculadora_dir = calculadora_dir or CALCULADORA_DIR
    cache_sqlite = cache_sqlite or CACHE_SQLITE

    with _lock:
        if os.path.exists(cache_sqlite) and not forcar:
            return cache_sqlite

        os.makedirs(os.path.dirname(cache_sqlite), exist_ok=True)
        tmp_extract = cache_sqlite + "_extract"
        _extrair_backend(calculadora_dir, tmp_extract)

        sqldir = os.path.join(tmp_extract, "flyway", "sql", "criacao")
        mandir = os.path.join(tmp_extract, "flyway", "sql", "manutencao")

        if os.path.exists(cache_sqlite):
            os.remove(cache_sqlite)
        conn = sqlite3.connect(cache_sqlite)
        cur = conn.cursor()

        arquivos = [
            os.path.join(sqldir, "beforeMigrate.sql"),
            os.path.join(sqldir, "B0001__sistema_tributario_completo.sql"),
        ]
        # as migrações de manutenção têm que rodar na ordem numérica, não
        # alfabética (V9 antes de V10)
        vfiles = sorted(
            glob.glob(os.path.join(mandir, "V*.sql")),
            key=lambda p: int(re.search(r"V(\d+)__", os.path.basename(p)).group(1)),
        )
        arquivos.extend(vfiles)

        erros = []
        for caminho in arquivos:
            with open(caminho, encoding="utf-8") as f:
                script = f.read()
            try:
                cur.executescript(script)
            except Exception as exc:
                erros.append((os.path.basename(caminho), str(exc)))

        conn.commit()
        conn.close()

        if erros:
            raise BaseRFBIndisponivel(
                f"Falha ao aplicar migrações da calculadora RFB: {erros}"
            )

        return cache_sqlite


def conectar(cache_sqlite: str | None = None) -> sqlite3.Connection:
    return sqlite3.connect(cache_sqlite or CACHE_SQLITE)


def conectar_base(forcar: bool = False) -> sqlite3.Connection:
    """Garante o cache montado e devolve a conexão — o uso normal do backend."""
    return conectar(montar_base(forcar=forcar))


def base_disponivel() -> bool:
    """Se dá pra montar/usar a base sem estourar — a tela avisa quando não dá."""
    if os.path.exists(CACHE_SQLITE):
        return True
    return os.path.isdir(CALCULADORA_DIR) and any(
        nome.lower() == "codigo-fonte-backend.zip" for nome in os.listdir(CALCULADORA_DIR)
    )
