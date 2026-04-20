"""
Agendador de coletas automáticas.

Suporta 3 modos independentes, que podem estar ativos ao mesmo tempo:

 • Diário         — todo dia às HH:MM
 • Semanal        — dias da semana escolhidos, no HH:MM configurado
 • Mensal         — até 3 dias do mês, cada um com seu próprio HH:MM
                    (dia inexistente no mês → roda no último dia do mês)

Recursos:
 • Catch-up: quando o coletor inicia (app aberto ou autostart), verifica se
   perdeu algum agendamento desde a última execução automática. Se sim,
   dispara uma coleta imediatamente.
 • Debounce: evita disparar duas vezes na mesma janela de 5 minutos, mesmo
   que vários modos coincidam no horário.

Funciona em thread daemon, verificando a cada 60s.

Convenção de dias da semana: Python weekday()
    0=Seg, 1=Ter, 2=Qua, 3=Qui, 4=Sex, 5=Sáb, 6=Dom
"""

import calendar
import threading
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from coletor.utils import get_logger, carregar_config, salvar_config

log = get_logger()


# ═══ Helpers ══════════════════════════════════════════════════════════════════

def _parse_horario(texto: str) -> Optional[Tuple[int, int]]:
    try:
        hh, mm = texto.split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:
        pass
    return None


def _proxima_diaria(depois_de: datetime, horario: str) -> Optional[datetime]:
    p = _parse_horario(horario)
    if not p:
        return None
    hh, mm = p
    alvo = depois_de.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if alvo > depois_de:
        return alvo
    return alvo + timedelta(days=1)


def _proxima_semanal(depois_de: datetime, dias: List[int], horario: str) -> Optional[datetime]:
    p = _parse_horario(horario)
    if not p or not dias:
        return None
    hh, mm = p
    for d in range(8):
        candidato = depois_de + timedelta(days=d)
        if candidato.weekday() in dias:
            alvo = candidato.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if alvo > depois_de:
                return alvo
    return None


def _proxima_mensal_um_dia(depois_de: datetime, dia_mes: int, horario: str) -> Optional[datetime]:
    p = _parse_horario(horario)
    if not p or not (1 <= dia_mes <= 31):
        return None
    hh, mm = p

    for offset in range(0, 3):
        ano = depois_de.year + ((depois_de.month - 1 + offset) // 12)
        mes = ((depois_de.month - 1 + offset) % 12) + 1
        ultimo_dia = calendar.monthrange(ano, mes)[1]
        dia_efetivo = min(dia_mes, ultimo_dia)
        try:
            alvo = datetime(ano, mes, dia_efetivo, hh, mm, 0, 0)
        except ValueError:
            continue
        if alvo > depois_de:
            return alvo
    return None


def proxima_execucao(cfg: Optional[dict] = None,
                     depois_de: Optional[datetime] = None) -> Optional[datetime]:
    cfg = cfg or carregar_config()
    if not cfg.get("envio_automatico", True):
        return None

    depois_de = depois_de or datetime.now()
    ag = cfg.get("agendamento", {}) or {}
    candidatos: List[datetime] = []

    d = ag.get("diario") or {}
    if d.get("ativo"):
        c = _proxima_diaria(depois_de, d.get("horario", "18:00"))
        if c: candidatos.append(c)

    s = ag.get("semanal") or {}
    if s.get("ativo") and s.get("dias"):
        c = _proxima_semanal(depois_de, list(s["dias"]), s.get("horario", "18:00"))
        if c: candidatos.append(c)

    m = ag.get("mensal") or {}
    if m.get("ativo") and m.get("dias"):
        for item in m["dias"]:
            try:
                c = _proxima_mensal_um_dia(depois_de, int(item["dia"]), item.get("horario", "18:00"))
                if c: candidatos.append(c)
            except (KeyError, ValueError, TypeError):
                continue

    if not candidatos:
        return None
    return min(candidatos)


def _modos_disparando_agora(cfg: dict, agora: datetime,
                            tolerancia_segundos: int = 90) -> List[str]:
    fontes: List[str] = []
    ag = cfg.get("agendamento", {}) or {}
    limite_inferior = agora - timedelta(seconds=tolerancia_segundos)

    def _casa(alvo: datetime) -> bool:
        return limite_inferior <= alvo <= agora

    d = ag.get("diario") or {}
    if d.get("ativo"):
        p = _parse_horario(d.get("horario", "18:00"))
        if p:
            hh, mm = p
            alvo = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if _casa(alvo):
                fontes.append(f"diario {hh:02d}:{mm:02d}")

    s = ag.get("semanal") or {}
    if s.get("ativo") and s.get("dias") and agora.weekday() in s["dias"]:
        p = _parse_horario(s.get("horario", "18:00"))
        if p:
            hh, mm = p
            alvo = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if _casa(alvo):
                fontes.append(f"semanal {hh:02d}:{mm:02d}")

    m = ag.get("mensal") or {}
    if m.get("ativo") and m.get("dias"):
        ultimo_dia = calendar.monthrange(agora.year, agora.month)[1]
        for item in m["dias"]:
            try:
                dia_cfg = int(item["dia"])
            except (KeyError, ValueError, TypeError):
                continue
            if not (1 <= dia_cfg <= 31):
                continue
            dia_efetivo = min(dia_cfg, ultimo_dia)
            if agora.day != dia_efetivo:
                continue
            p = _parse_horario(item.get("horario", "18:00"))
            if not p:
                continue
            hh, mm = p
            alvo = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if _casa(alvo):
                fontes.append(f"mensal dia {dia_efetivo} {hh:02d}:{mm:02d}")

    return fontes


# ═══ Classe principal ═════════════════════════════════════════════════════════

class Agendador:
    """Thread que verifica agendamentos e dispara a ação de coleta."""

    INTERVALO_TICK_SEGUNDOS = 60
    DEBOUNCE_MINUTOS = 5

    def __init__(self, acao_coleta: Callable[[], None]):
        self.acao_coleta = acao_coleta
        self._thread: Optional[threading.Thread] = None
        self._parar = threading.Event()
        self._ultimo_disparo: Optional[datetime] = None
        self._catchup_checado = False

    def iniciar(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._parar.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="Agendador")
        self._thread.start()
        log.info("Agendador iniciado")

    def parar(self) -> None:
        self._parar.set()
        log.info("Agendador parado")

    def proxima_execucao(self) -> Optional[datetime]:
        return proxima_execucao()

    def _loop(self) -> None:
        if self._parar.wait(5):
            return

        while not self._parar.is_set():
            try:
                if not self._catchup_checado:
                    self._catchup_checado = True
                    self._fazer_catchup()

                self._tick_normal()
            except Exception as e:
                log.error("Erro no loop do agendador: %s", e)

            if self._parar.wait(self.INTERVALO_TICK_SEGUNDOS):
                break

    def _fazer_catchup(self) -> None:
        cfg = carregar_config()
        if not cfg.get("envio_automatico", True):
            return

        ultimo_iso = cfg.get("ultimo_envio_automatico")
        agora = datetime.now()

        if not ultimo_iso:
            log.info("Sem registro de envio automático anterior — catch-up pulado")
            return

        try:
            ultimo = datetime.fromisoformat(ultimo_iso)
        except Exception:
            log.warning("ultimo_envio_automatico inválido: %s", ultimo_iso)
            return

        proxima_apos_ultimo = proxima_execucao(cfg, ultimo)
        if proxima_apos_ultimo is None:
            return

        if proxima_apos_ultimo <= agora:
            log.info("Catch-up: agendamento perdido em %s (última exec: %s)",
                     proxima_apos_ultimo.isoformat(timespec="minutes"),
                     ultimo_iso)
            self._disparar("catchup")

    def _tick_normal(self) -> None:
        cfg = carregar_config()
        if not cfg.get("envio_automatico", True):
            return

        agora = datetime.now()

        if self._ultimo_disparo and (agora - self._ultimo_disparo) < timedelta(minutes=self.DEBOUNCE_MINUTOS):
            return

        fontes = _modos_disparando_agora(cfg, agora, tolerancia_segundos=90)
        if fontes:
            self._disparar(" + ".join(fontes))

    def _disparar(self, fonte: str) -> None:
        self._ultimo_disparo = datetime.now()
        log.info("Agendador disparando coleta (fonte: %s)", fonte)

        try:
            cfg = carregar_config()
            cfg["ultimo_envio_automatico"] = self._ultimo_disparo.isoformat(timespec="seconds")
            salvar_config(cfg)
        except Exception as e:
            log.error("Não foi possível persistir ultimo_envio_automatico: %s", e)

        try:
            self.acao_coleta()
        except Exception as e:
            log.error("Erro executando acao_coleta: %s", e)
