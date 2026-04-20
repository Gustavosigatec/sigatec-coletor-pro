"""
Utilitários: caminhos em %APPDATA%, logger rotativo, config persistente do usuário.
"""
import os
import json
import socket
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from coletor import config


# ═══ Caminhos ═════════════════════════════════════════════════════════════════

def pasta_appdata() -> Path:
    """Retorna (criando se preciso) a pasta de dados do coletor.

    Windows: %APPDATA%\\SigatecColetorPro
    Linux/macOS (dev): ~/.sigatec-coletor-pro
    """
    if os.name == "nt":
        base = os.getenv("APPDATA") or os.path.expanduser("~")
        pasta = Path(base) / config.NOME_PASTA_APPDATA
    else:
        pasta = Path.home() / (".sigatec-coletor-pro")
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def caminho_config() -> Path:
    return pasta_appdata() / config.ARQUIVO_CONFIG


def caminho_log() -> Path:
    return pasta_appdata() / config.ARQUIVO_LOG


def caminho_installation_id() -> Path:
    return pasta_appdata() / config.ARQUIVO_INSTALLATION_ID


# ═══ Installation ID ══════════════════════════════════════════════════════════

_installation_id_cache: str = ""


def get_installation_id() -> str:
    """Retorna o UUID desta instalação. Cria na primeira chamada."""
    import uuid

    global _installation_id_cache
    if _installation_id_cache:
        return _installation_id_cache

    path = caminho_installation_id()
    try:
        if path.exists():
            conteudo = path.read_text(encoding="utf-8").strip()
            if conteudo:
                try:
                    _installation_id_cache = str(uuid.UUID(conteudo))
                    return _installation_id_cache
                except (ValueError, TypeError):
                    get_logger().warning("installation_id.txt inválido, gerando novo")

        novo = str(uuid.uuid4())
        path.write_text(novo, encoding="utf-8")
        _installation_id_cache = novo
        get_logger().info("Installation ID gerado: %s", novo)
        return novo
    except Exception as e:
        get_logger().error("Erro lendo/criando installation_id: %s", e)
        if not _installation_id_cache:
            _installation_id_cache = str(uuid.uuid4())
        return _installation_id_cache


# ═══ Logger ═══════════════════════════════════════════════════════════════════

_logger_cache = None


def get_logger() -> logging.Logger:
    global _logger_cache
    if _logger_cache:
        return _logger_cache

    logger = logging.getLogger("sigatec_coletor_pro")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = RotatingFileHandler(
            caminho_log(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                              "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)

    _logger_cache = logger
    return logger


# ═══ Config persistente ═══════════════════════════════════════════════════════

_AGENDAMENTO_PADRAO = {
    "diario": {
        "ativo": True,
        "horario": config.HORARIO_ENVIO_PADRAO,
    },
    "semanal": {
        "ativo": False,
        "dias": [],
        "horario": config.HORARIO_ENVIO_PADRAO,
    },
    "mensal": {
        "ativo": False,
        "dias": [],
    },
}

_CONFIG_PADRAO = {
    "nome_agente":             "",
    "envio_automatico":        True,
    "agendamento":             dict(_AGENDAMENTO_PADRAO),
    "iniciar_com_windows":     True,
    "ips_conhecidos":          [],
    "ultima_coleta":           None,
    "ultimo_envio_ok":         None,
    "ultimo_envio_status":     None,
    "ultimo_envio_automatico": None,
    "tunnel_ativo":            True,
}


def _migrar_config(cfg: dict) -> dict:
    if "agendamento" not in cfg or not isinstance(cfg.get("agendamento"), dict):
        ag = {
            "diario": {
                "ativo": True,
                "horario": cfg.get("horario_envio") or config.HORARIO_ENVIO_PADRAO,
            },
            "semanal": {"ativo": False, "dias": [], "horario": config.HORARIO_ENVIO_PADRAO},
            "mensal":  {"ativo": False, "dias": []},
        }
        cfg["agendamento"] = ag
        get_logger().info("Config migrada: horario_envio → agendamento.diario")

    ag = cfg["agendamento"]
    ag.setdefault("diario", {})
    ag["diario"].setdefault("ativo", True)
    ag["diario"].setdefault("horario", config.HORARIO_ENVIO_PADRAO)

    ag.setdefault("semanal", {})
    ag["semanal"].setdefault("ativo", False)
    ag["semanal"].setdefault("dias", [])
    ag["semanal"].setdefault("horario", config.HORARIO_ENVIO_PADRAO)

    ag.setdefault("mensal", {})
    ag["mensal"].setdefault("ativo", False)
    ag["mensal"].setdefault("dias", [])

    return cfg


def carregar_config() -> dict:
    path = caminho_config()

    if not path.exists():
        cfg = json.loads(json.dumps(_CONFIG_PADRAO))
        try:
            cfg["nome_agente"] = socket.gethostname()
        except Exception:
            cfg["nome_agente"] = "PC"
        salvar_config(cfg)
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in _CONFIG_PADRAO.items():
            cfg.setdefault(k, v)
        cfg = _migrar_config(cfg)
        return cfg
    except Exception as e:
        get_logger().error("Erro lendo config, usando padrões: %s", e)
        return json.loads(json.dumps(_CONFIG_PADRAO))


def salvar_config(cfg: dict) -> None:
    path = caminho_config()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        get_logger().error("Erro salvando config: %s", e)
