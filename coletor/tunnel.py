"""
Módulo de túnel WebSocket reverso.

Mantém uma conexão persistente com o servidor central e permite que o
servidor faça requisições HTTP para o painel web das impressoras através
do agente — sem precisar abrir portas no cliente.

Fluxo:
  Servidor → WS → Agente → HTTP → Impressora → HTTP → Agente → WS → Servidor

Protocolo (JSON sobre WebSocket):
  Entrada (servidor → agente):
    {"type": "proxy_request", "id": "req-xxx", "ip": "192.168.1.50",
     "path": "/", "method": "GET", "headers": {}, "https": false}

  Saída (agente → servidor):
    {"type": "proxy_response", "id": "req-xxx", "status": 200,
     "headers": {}, "body": "<base64>", "content_type": "text/html"}

  Heartbeat (agente → servidor):
    {"type": "heartbeat", "installation_id": "...", "timestamp": "..."}

  Lista de impressoras (agente → servidor):
    {"type": "printer_list", "installation_id": "...", "printers": [...]}
"""

import base64
import json
import ssl
import threading
import time
import urllib.request
from datetime import datetime
from typing import Optional
from urllib.request import Request as _Request
from urllib.error import URLError as _URLError

_SSL_NO_VERIFY = ssl.create_default_context()
_SSL_NO_VERIFY.check_hostname = False
_SSL_NO_VERIFY.verify_mode = ssl.CERT_NONE


class _SemRedirect(urllib.request.HTTPErrorProcessor):
    """Impede que urllib siga redirects automaticamente.
    O servidor é responsável por tratar redirecionamentos HTTP→HTTPS."""
    def http_response(self, request, response):
        return response
    https_response = http_response


# Openers reutilizáveis (sem redirect, com/sem SSL)
_OPENER_HTTP = urllib.request.build_opener(_SemRedirect())
_OPENER_HTTPS = urllib.request.build_opener(
    _SemRedirect(),
    urllib.request.HTTPSHandler(context=_SSL_NO_VERIFY),
)

from coletor import config
from coletor.utils import get_logger, get_installation_id, carregar_config

log = get_logger()

# Indica estado do túnel para a UI
_status_tunnel: str = "desconectado"
_status_lock = threading.Lock()


def get_status() -> str:
    """Retorna o estado atual do túnel: 'conectado', 'reconectando' ou 'desconectado'."""
    with _status_lock:
        return _status_tunnel


def _set_status(status: str) -> None:
    global _status_tunnel
    with _status_lock:
        _status_tunnel = status



def _fazer_request_impressora(ip: str, path: str, method: str,
                               req_headers: dict, https: bool = False,
                               body_b64: str = "") -> dict:
    """Faz uma requisição HTTP/HTTPS para o painel web da impressora.

    Não segue redirects — o servidor decide se deve re-emitir via HTTPS.
    Retorna dict com status, headers (lowercase), body (base64), content_type.
    """
    # Normaliza path
    path = path.lstrip("/") if path else ""
    scheme = "https" if https else "http"
    url = f"{scheme}://{ip}/{path}"

    # Decodifica body do POST (se houver)
    req_body: bytes | None = None
    if body_b64:
        try:
            req_body = base64.b64decode(body_b64)
        except Exception:
            req_body = None

    # Cabeçalhos seguros para repassar (exclui hop-by-hop e Connection)
    _EXCLUIR_HEADERS = {
        "host", "connection", "transfer-encoding", "upgrade",
        "proxy-authenticate", "proxy-authorization", "te", "trailer",
    }
    safe_headers = {
        k: v for k, v in req_headers.items()
        if k.lower() not in _EXCLUIR_HEADERS
    }
    # Força host correto
    safe_headers["Host"] = ip

    opener = _OPENER_HTTPS if https else _OPENER_HTTP

    try:
        req = _Request(url, data=req_body, method=method.upper(), headers=safe_headers)
        with opener.open(req, timeout=10) as resp:
            status = resp.status
            # Retorna headers em lowercase para o servidor detectar Location etc.
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
            content_type = resp_headers.get("content-type", "")
    except _URLError as e:
        log.debug("Proxy HTTP erro %s%s: %s", ip, path, e)
        return {
            "status": 502,
            "headers": {},
            "body": base64.b64encode(str(e).encode()).decode(),
            "content_type": "text/plain",
        }
    except Exception as e:
        log.debug("Proxy HTTP erro inesperado %s%s: %s", ip, path, e)
        return {
            "status": 500,
            "headers": {},
            "body": base64.b64encode(str(e).encode()).decode(),
            "content_type": "text/plain",
        }

    return {
        "status": status,
        "headers": resp_headers,
        "body": base64.b64encode(body).decode(),
        "content_type": content_type,
    }


def _obter_impressoras_conhecidas() -> list:
    """Retorna a lista de impressoras conhecidas (IPs descobertos via SNMP)."""
    try:
        cfg = carregar_config()
        ips = cfg.get("ips_conhecidos") or []
        return [{"ip": ip} for ip in ips]
    except Exception:
        return []


class TunnelClient:
    """Cliente de túnel WebSocket que mantém conexão persistente com o servidor."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._parar = threading.Event()
        self._ws = None
        self._ultima_lista_impressoras: list = []

    def iniciar(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._parar.clear()
        self._thread = threading.Thread(
            target=self._loop_reconexao, daemon=True, name="TunnelWS"
        )
        self._thread.start()
        log.info("Tunnel iniciado (destino: %s)", config.TUNNEL_WS_URL)

    def parar(self) -> None:
        self._parar.set()
        self._fechar_ws()
        log.info("Tunnel parado")

    # ─── Reconexão com backoff exponencial ───────────────────────────────────

    def _loop_reconexao(self) -> None:
        backoff = config.TUNNEL_BACKOFF_MIN
        while not self._parar.is_set():
            try:
                _set_status("reconectando")
                self._conectar_e_rodar()
                # Se chegou aqui sem exceção, foi parado intencionalmente
                break
            except Exception as e:
                if self._parar.is_set():
                    break
                log.warning("Tunnel desconectado (%s). Reconectando em %ds...", e, backoff)
                _set_status("desconectado")
                if self._parar.wait(backoff):
                    break
                # Backoff exponencial com teto
                backoff = min(backoff * 2, config.TUNNEL_BACKOFF_MAX)
            else:
                backoff = config.TUNNEL_BACKOFF_MIN

        _set_status("desconectado")
        log.info("Loop de reconexão encerrado")

    # ─── Sessão WebSocket ────────────────────────────────────────────────────

    def _conectar_e_rodar(self) -> None:
        try:
            import websockets.sync.client as _wsc
        except ImportError:
            # websockets >= 12 usa sync.client; versão anterior usa connect direto
            try:
                import websockets
                _wsc = websockets
            except ImportError:
                log.error("Pacote 'websockets' não instalado. "
                          "Execute: pip install websockets")
                self._parar.wait(60)
                return

        installation_id = get_installation_id()
        extra_headers = {
            "X-API-Key": config.INGEST_KEY,
            "X-Installation-ID": installation_id,
        }

        log.info("Conectando ao tunnel WS: %s", config.TUNNEL_WS_URL)

        with _wsc.connect(
            config.TUNNEL_WS_URL,
            additional_headers=extra_headers,
            ping_interval=None,      # Heartbeat manual
            open_timeout=15,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            _set_status("conectado")
            backoff_ref = [config.TUNNEL_BACKOFF_MIN]
            backoff_ref[0] = config.TUNNEL_BACKOFF_MIN
            log.info("Tunnel conectado (installation_id=%s...)", installation_id[:8])

            # Envia lista de impressoras ao conectar
            self._enviar_lista_impressoras(ws, installation_id)

            # Inicia thread de heartbeat
            hb_stop = threading.Event()
            hb_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(ws, installation_id, hb_stop),
                daemon=True,
                name="TunnelHeartbeat",
            )
            hb_thread.start()

            try:
                self._loop_mensagens(ws, installation_id)
            finally:
                hb_stop.set()
                hb_thread.join(timeout=3)
                self._ws = None

    def _loop_mensagens(self, ws, installation_id: str) -> None:
        """Processa mensagens recebidas do servidor."""
        while not self._parar.is_set():
            try:
                raw = ws.recv(timeout=5)
            except TimeoutError:
                # Verifica se lista de impressoras mudou
                self._checar_lista_impressoras(ws, installation_id)
                continue
            except Exception:
                raise

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("Tunnel: mensagem não-JSON ignorada")
                continue

            tipo = msg.get("type")

            if tipo == "proxy_request":
                self._handle_proxy_request(ws, msg)
            elif tipo == "ping":
                self._enviar(ws, {"type": "pong"})
            else:
                log.debug("Tunnel: tipo desconhecido '%s'", tipo)

    # ─── Heartbeat ────────────────────────────────────────────────────────────

    def _heartbeat_loop(self, ws, installation_id: str, stop: threading.Event) -> None:
        while not stop.is_set() and not self._parar.is_set():
            if stop.wait(config.TUNNEL_HEARTBEAT_INTERVAL):
                break
            try:
                self._enviar(ws, {
                    "type": "heartbeat",
                    "installation_id": installation_id,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                })
            except Exception as e:
                log.debug("Heartbeat falhou: %s", e)
                break

    # ─── Proxy de requisições ────────────────────────────────────────────────

    def _handle_proxy_request(self, ws, msg: dict) -> None:
        req_id = msg.get("id", "")
        ip = msg.get("ip", "")
        path = msg.get("path", "/")
        method = msg.get("method", "GET")
        headers = msg.get("headers", {}) or {}
        https = bool(msg.get("https", False))
        body_b64 = msg.get("body", "")

        log.debug("Proxy request: %s %s://%s%s", method, "https" if https else "http", ip, path)

        # Executa em thread pra não bloquear o loop principal
        threading.Thread(
            target=self._executar_proxy,
            args=(ws, req_id, ip, path, method, headers, https, body_b64),
            daemon=True,
        ).start()

    def _executar_proxy(self, ws, req_id: str, ip: str, path: str,
                        method: str, headers: dict, https: bool = False,
                        body_b64: str = "") -> None:
        resultado = _fazer_request_impressora(ip, path, method, headers, https, body_b64)
        resposta = {
            "type": "proxy_response",
            "id": req_id,
            **resultado,
        }
        try:
            self._enviar(ws, resposta)
        except Exception as e:
            log.debug("Erro enviando proxy_response: %s", e)

    # ─── Lista de impressoras ────────────────────────────────────────────────

    def _enviar_lista_impressoras(self, ws, installation_id: str) -> None:
        impressoras = _obter_impressoras_conhecidas()
        self._ultima_lista_impressoras = impressoras
        msg = {
            "type": "printer_list",
            "installation_id": installation_id,
            "agent_name": carregar_config().get("nome_agente", ""),
            "printers": impressoras,
        }
        self._enviar(ws, msg)
        log.info("Tunnel: enviada lista com %d impressora(s)", len(impressoras))

    def _checar_lista_impressoras(self, ws, installation_id: str) -> None:
        """Envia lista atualizada se as impressoras mudaram desde o último envio."""
        try:
            atual = _obter_impressoras_conhecidas()
            if atual != self._ultima_lista_impressoras:
                self._enviar_lista_impressoras(ws, installation_id)
        except Exception:
            pass

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _enviar(self, ws, dados: dict) -> None:
        ws.send(json.dumps(dados, ensure_ascii=False))

    def _fechar_ws(self) -> None:
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None


# Instância singleton usada pelo agendador e UI
_tunnel_instance: Optional[TunnelClient] = None
_tunnel_lock = threading.Lock()


def get_tunnel() -> TunnelClient:
    global _tunnel_instance
    with _tunnel_lock:
        if _tunnel_instance is None:
            _tunnel_instance = TunnelClient()
    return _tunnel_instance


def iniciar_tunnel() -> None:
    """Inicia o túnel se estiver habilitado na config."""
    cfg = carregar_config()
    tunnel_ativo = cfg.get("tunnel_ativo", True) and config.TUNNEL_ATIVO
    if not tunnel_ativo:
        log.info("Tunnel desabilitado na configuração")
        return
    get_tunnel().iniciar()


def parar_tunnel() -> None:
    """Para o túnel se estiver rodando."""
    with _tunnel_lock:
        if _tunnel_instance:
            _tunnel_instance.parar()
