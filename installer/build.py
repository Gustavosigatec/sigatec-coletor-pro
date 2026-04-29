"""
Script de build do Sigatec Coletor Pro.

Gera um executavel unico com a INGEST KEY ja embutida e XOR-ofuscada
(veja coletor/config.py para detalhes da ofuscacao).

Uso:

    python installer/build.py --ingest-key "sua-chave-aqui"
    python installer/build.py --ingest-key "..." --url "https://..."
    python installer/build.py --ingest-key "..." --clean
    python installer/build.py --ingest-key "..." --suffix Win7

Pre-requisitos (instalar uma vez antes do build):

    pip install pyinstaller pyinstaller-hooks-contrib

Versionamento automatico:
    O nome do .exe gerado SEMPRE inclui a data do build.
    Exemplo: SigatecColetorPro_2026-04-28.exe
             SigatecColetorPro_Win7_2026-04-28.exe (com --suffix Win7)

Arquivamento:
    Antes de cada build, qualquer .exe ja presente em dist/ e movido para
    dist/_versoes_anteriores/ — voce nunca perde uma versao por engano.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = ROOT / "coletor" / "config.py"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
ARCHIVE_DIR = DIST_DIR / "_versoes_anteriores"


def _arquivar_versoes_anteriores():
    """Move qualquer .exe em dist/ (raiz) para dist/_versoes_anteriores/.
    Em caso de nome ja existente no arquivo, adiciona timestamp para nao
    sobrescrever."""
    if not DIST_DIR.exists():
        return
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for exe in DIST_DIR.glob("*.exe"):
        target = ARCHIVE_DIR / exe.name
        if target.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = ARCHIVE_DIR / f"{exe.stem}_{ts}{exe.suffix}"
        print(f"  Arquivando: {exe.name} -> _versoes_anteriores/{target.name}")
        exe.rename(target)


def _nome_versionado(base: str, suffix: str | None) -> str:
    """Monta nome do .exe com data: base[_suffix]_YYYY-MM-DD"""
    partes = [base]
    if suffix:
        partes.append(suffix)
    partes.append(datetime.now().strftime("%Y-%m-%d"))
    return "_".join(partes)


def main():
    parser = argparse.ArgumentParser(description="Build do Sigatec Coletor Pro")
    parser.add_argument("--ingest-key", "--api-key", dest="ingest_key",
                        required=True,
                        help="INGEST KEY do Sigatec (escopada pro /bc/ingest)")
    parser.add_argument("--url", default=None,
                        help="URL base do Sigatec (sobrescreve o padrão)")
    parser.add_argument("--tunnel-url", default=None,
                        help="URL WebSocket do tunnel (sobrescreve o padrão)")
    parser.add_argument("--clean", action="store_true",
                        help="Limpa dist/ e build/ antes")
    parser.add_argument("--suffix", default=None,
                        help="Sufixo no nome do .exe final. Ex: Win7 → SigatecColetorPro_Win7.exe")
    args = parser.parse_args()

    nome_exe = _nome_versionado("SigatecColetorPro", args.suffix)
    print(f"Nome do .exe gerado: {nome_exe}.exe")

    # Arquiva qualquer .exe atual antes de gerar o novo
    print("\nArquivando versoes anteriores em dist/_versoes_anteriores/...")
    _arquivar_versoes_anteriores()

    if args.clean:
        # --clean limpa apenas build/, dist/ ja foi tratado pelo arquivamento
        if BUILD_DIR.exists():
            print(f"Limpando {BUILD_DIR}...")
            shutil.rmtree(BUILD_DIR)

    sys.path.insert(0, str(ROOT))
    from coletor.config import _xor_encode  # noqa: E402

    chave_ofuscada = _xor_encode(args.ingest_key)
    print(f"Chave ofuscada ({len(chave_ofuscada)} chars) preparada.")

    original_config = CONFIG_PY.read_text(encoding="utf-8")
    novo = original_config

    novo = re.sub(
        r'_INGEST_KEY_OFUSCADA\s*=\s*"[^"]*"',
        f'_INGEST_KEY_OFUSCADA = "{chave_ofuscada}"',
        novo,
        flags=re.MULTILINE,
    )

    if args.url:
        novo = re.sub(
            r'SIGATEC_URL\s*=\s*os\.getenv\(\s*"SIGATEC_URL",\s*"[^"]*"\s*\)',
            f'SIGATEC_URL = os.getenv("SIGATEC_URL", "{args.url}")',
            novo,
            flags=re.MULTILINE,
        )

    if args.tunnel_url:
        novo = re.sub(
            r'TUNNEL_WS_URL\s*=\s*os\.getenv\(\s*"TUNNEL_WS_URL",\s*"[^"]*"\s*\)',
            f'TUNNEL_WS_URL = os.getenv("TUNNEL_WS_URL", "{args.tunnel_url}")',
            novo,
            flags=re.MULTILINE,
        )

    if novo == original_config:
        print("AVISO: substituições não tiveram efeito — checa os placeholders em config.py.")
        sys.exit(1)

    CONFIG_PY.write_text(novo, encoding="utf-8")

    try:
        print("Rodando PyInstaller...")
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--onefile",
            "--windowed",
            "--noconfirm",
            "--name", nome_exe,
            "--additional-hooks-dir", str(ROOT / "installer" / "hooks"),
            "--runtime-hook", str(ROOT / "installer" / "hooks" / "rthook_puresnmp.py"),

            # ── websockets ────────────────────────────────────────────────────
            # Importa submódulos em runtime; collect-all garante que todos
            # os arquivos .py do pacote vão para o bundle.
            "--collect-all", "websockets",
            "--hidden-import", "websockets",
            "--hidden-import", "websockets.sync",
            "--hidden-import", "websockets.sync.client",
            "--hidden-import", "websockets.asyncio.client",
            "--hidden-import", "websockets.legacy.client",

            # ── pystray ───────────────────────────────────────────────────────
            # Backend Win32 é escolhido em runtime; sem collect-all o import
            # silenciosamente falha e o ícone de bandeja não aparece.
            "--collect-all", "pystray",
            "--hidden-import", "pystray",
            "--hidden-import", "pystray._util.win32",

            # ── Pillow ────────────────────────────────────────────────────────
            # collect-all inclui fontes bitmap internas (usadas em draw.text
            # sem fonte explícita) e demais dados de suporte do pacote.
            "--collect-all", "PIL",
            "--hidden-import", "PIL",
            "--hidden-import", "PIL.Image",
            "--hidden-import", "PIL.ImageDraw",
            "--hidden-import", "PIL.ImageFont",

            # ── pywin32 ───────────────────────────────────────────────────────
            # PyInstaller NÃO detecta automaticamente os DLLs do pywin32
            # (pywintypes3X.dll). O pacote pyinstaller-hooks-contrib resolve
            # isso; os hidden-imports abaixo cobrem os imports diretos.
            "--hidden-import", "win32api",
            "--hidden-import", "win32con",
            "--hidden-import", "win32gui",
            "--hidden-import", "win32process",
            "--hidden-import", "win32security",
            "--hidden-import", "win32service",
            "--hidden-import", "pywintypes",
            "--hidden-import", "winreg",

            # ── certifi (SSL) ─────────────────────────────────────────────────
            # requests e urllib usam certifi para localizar o bundle de CAs.
            # Sem collect-all o path do cacert.pem fica errado no .exe.
            "--collect-all", "certifi",
            "--hidden-import", "certifi",

            # ── puresnmp 1.x ──────────────────────────────────────────────────
            # Hook em installer/hooks/hook-puresnmp.py copia o .dist-info
            # e coleta todos os arquivos do pacote.
            "--hidden-import", "puresnmp",
            "--hidden-import", "puresnmp.api",
            "--hidden-import", "puresnmp.exc",
            "--hidden-import", "puresnmp.types",
            "--hidden-import", "puresnmp.x690",
            "--hidden-import", "puresnmp.x690.types",
            "--hidden-import", "x690",

            # ── tkinter ───────────────────────────────────────────────────────
            # Embutido no Python, mas submódulos podem não ser detectados
            # com --windowed.
            "--hidden-import", "tkinter",
            "--hidden-import", "tkinter.ttk",
            "--hidden-import", "tkinter.messagebox",

            # ── stdlib raramente detectados ───────────────────────────────────
            "--hidden-import", "logging.handlers",
            "--hidden-import", "email.mime.text",
            "--hidden-import", "email.mime.multipart",

            "main.py",
        ]
        env = dict(os.environ)
        env.pop("SIGATEC_INGEST_KEY", None)
        env.pop("SIGATEC_API_KEY", None)
        subprocess.check_call(cmd, cwd=str(ROOT), env=env)
        exe_path = DIST_DIR / f"{nome_exe}.exe"
        if exe_path.exists():
            print(f"\nBuild concluído: {exe_path}")
            print(f"   Tamanho: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
        else:
            print("\nBuild terminou mas o executável não foi encontrado.")
            sys.exit(1)
    finally:
        print("Revertendo config.py (placeholder restaurado)...")
        CONFIG_PY.write_text(original_config, encoding="utf-8")


if __name__ == "__main__":
    main()
