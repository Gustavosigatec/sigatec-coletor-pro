"""
Script de build do Sigatec Coletor Pro.

Gera um executável único (SigatecColetorPro.exe) com a INGEST KEY já embutida
e XOR-ofuscada (veja coletor/config.py para detalhes da ofuscação).

Uso:

    python installer/build.py --ingest-key "sua-chave-aqui"
    python installer/build.py --ingest-key "..." --url "https://..."
    python installer/build.py --ingest-key "..." --clean

Resultado: dist/SigatecColetorPro.exe
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = ROOT / "coletor" / "config.py"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"


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
    args = parser.parse_args()

    if args.clean:
        for p in [DIST_DIR, BUILD_DIR]:
            if p.exists():
                print(f"Limpando {p}...")
                shutil.rmtree(p)

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
            "--name", "SigatecColetorPro",
            "--hidden-import", "websockets",
            "--hidden-import", "websockets.sync",
            "--hidden-import", "websockets.sync.client",
            "main.py",
        ]
        env = dict(os.environ)
        env.pop("SIGATEC_INGEST_KEY", None)
        env.pop("SIGATEC_API_KEY", None)
        subprocess.check_call(cmd, cwd=str(ROOT), env=env)
        exe_path = DIST_DIR / "SigatecColetorPro.exe"
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
