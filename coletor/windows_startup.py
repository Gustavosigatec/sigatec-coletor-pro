"""
Integração com Windows: cadastra/descadastra o coletor para iniciar com o sistema.

Usa HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
(não precisa de privilégio admin).
"""
import os
import sys
from coletor import config
from coletor.utils import get_logger

log = get_logger()


def _pegar_caminho_executavel() -> str:
    """Retorna o caminho do executável atual (compatível com PyInstaller)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "main.py")
    return f'pythonw "{os.path.abspath(main_py)}"'


def cadastrar_autoinicio() -> bool:
    """Adiciona o coletor à inicialização do Windows do usuário atual."""
    if os.name != "nt":
        return False
    try:
        import winreg
        exe = _pegar_caminho_executavel()
        valor = f'"{exe}" --minimizado' if getattr(sys, "frozen", False) else f'{exe} --minimizado'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, config.REG_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as chave:
            winreg.SetValueEx(chave, config.REG_RUN_NAME, 0, winreg.REG_SZ, valor)
        log.info("Autoinicio cadastrado: %s", valor)
        return True
    except Exception as e:
        log.error("Falha cadastrando autoinicio: %s", e)
        return False


def descadastrar_autoinicio() -> bool:
    """Remove o coletor da inicialização do Windows."""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, config.REG_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as chave:
            try:
                winreg.DeleteValue(chave, config.REG_RUN_NAME)
            except FileNotFoundError:
                pass
        log.info("Autoinicio removido")
        return True
    except Exception as e:
        log.error("Falha removendo autoinicio: %s", e)
        return False


def esta_no_autoinicio() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, config.REG_RUN_KEY, 0,
                            winreg.KEY_READ) as chave:
            try:
                valor, _ = winreg.QueryValueEx(chave, config.REG_RUN_NAME)
                return bool(valor)
            except FileNotFoundError:
                return False
    except Exception:
        return False
