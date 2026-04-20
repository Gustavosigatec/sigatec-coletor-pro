"""
Interface gráfica do Sigatec Coletor Pro.

Design: janela única, pequena, objetiva.
- Cabeçalho com status (última coleta, últimas impressoras)
- Botão "Coletar e Enviar Agora"
- Botão "Varrer Rede" (descobre IPs Brother)
- Configurações (horário de envio, nome do agente, autostart, tunnel)
- Indicador de status do túnel (conectado/desconectado)
- Ao fechar: minimiza para bandeja (não encerra)
"""
import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

from coletor import config
from coletor.utils import get_logger, carregar_config, salvar_config
from coletor.snmp_reader import varrer_rede, coletar_de_ips, LeituraImpressora
from coletor.usb_reader import coletar_usb
from coletor.api_client import enviar_leituras, testar_conexao, SigatecAPIError
from coletor.agendador import Agendador, proxima_execucao as _proxima_execucao
from coletor import windows_startup
from coletor import tunnel as _tunnel_mod

log = get_logger()

# Cores para o indicador de status do túnel
_COR_CONECTADO    = "#27ae60"
_COR_RECONECTANDO = "#e67e22"
_COR_DESCONECTADO = "#e74c3c"


# ═══ Componentes auxiliares ═══════════════════════════════════════════════════

class IconeBandeja:
    """Wrapper opcional para ícone na bandeja do sistema (pystray)."""

    def __init__(self, app):
        self.app = app
        self.icon = None
        try:
            import pystray
            from PIL import Image
            self._pystray = pystray
            self._Image = Image
        except ImportError:
            log.warning("pystray/Pillow indisponíveis — bandeja não será criada")
            self._pystray = None

    def criar(self):
        if not self._pystray:
            return

        from PIL import Image, ImageDraw
        img = Image.new("RGB", (64, 64), color=(0, 86, 179))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 63, 63], outline=(255, 255, 255), width=2)
        draw.text((14, 10), "SP", fill=(255, 255, 255))

        pystray = self._pystray
        menu = pystray.Menu(
            pystray.MenuItem("Abrir", self._abrir, default=True),
            pystray.MenuItem("Coletar agora", self._coletar),
            pystray.MenuItem(
                "Tunnel",
                pystray.Menu(
                    pystray.MenuItem(
                        lambda item: f"Status: {_tunnel_mod.get_status()}",
                        None,
                        enabled=False,
                    ),
                )
            ),
        )
        self.icon = pystray.Icon(config.APP_NOME, img, config.APP_NOME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def parar(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass

    def _abrir(self, icon=None, item=None):
        self.app.mostrar_janela()

    def _coletar(self, icon=None, item=None):
        self.app.coletar_e_enviar_async()

    def _sair(self, icon=None, item=None):
        self.app.encerrar()


# ═══ App principal ════════════════════════════════════════════════════════════

class AppColetor:
    def __init__(self, iniciar_minimizado: bool = False):
        self.cfg = carregar_config()
        self.fila_ui = queue.Queue()

        self.root = tk.Tk()
        self.root.title(f"{config.APP_NOME}  v{config.APP_VERSAO}")
        self.root.geometry("640x580")
        self.root.minsize(580, 520)

        self._montar_ui()
        self._aplicar_estilo()

        self.root.protocol("WM_DELETE_WINDOW", self.minimizar_para_bandeja)

        self.bandeja = IconeBandeja(self)
        self.bandeja.criar()

        # Agendador de coletas
        self.agendador = Agendador(self.coletar_e_enviar_async)
        self.agendador.iniciar()

        # Inicia o túnel WebSocket
        _tunnel_mod.iniciar_tunnel()

        self._atualizar_status()

        self.root.after(100, self._processar_fila)
        self.root.after(1000, self._tick_proxima_execucao)
        self.root.after(2000, self._tick_status_tunnel)

        if iniciar_minimizado:
            self.root.after(100, self.minimizar_para_bandeja)

    def _tick_proxima_execucao(self):
        try:
            self._atualizar_proxima_execucao()
        except Exception:
            pass
        self.root.after(60_000, self._tick_proxima_execucao)

    def _tick_status_tunnel(self):
        """Atualiza o indicador de status do túnel a cada 2s."""
        try:
            self._atualizar_indicador_tunnel()
        except Exception:
            pass
        self.root.after(2_000, self._tick_status_tunnel)

    # ─── Construção da UI ────────────────────────────────────────────────────

    def _aplicar_estilo(self):
        style = ttk.Style()
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except Exception:
            pass
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _montar_ui(self):
        pad = {"padx": 12, "pady": 6}

        # Cabeçalho
        topo = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        topo.pack(fill="x")

        linha_titulo = ttk.Frame(topo)
        linha_titulo.pack(fill="x")
        ttk.Label(linha_titulo, text="Sigatec Coletor Pro",
                  font=("Segoe UI", 16, "bold")).pack(side="left", anchor="w")

        # Indicador de tunnel (canto direito do cabeçalho)
        self.frm_tunnel_indicator = ttk.Frame(linha_titulo)
        self.frm_tunnel_indicator.pack(side="right", anchor="e")
        ttk.Label(self.frm_tunnel_indicator, text="Acesso Remoto:",
                  font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self.lbl_tunnel_status = tk.Label(
            self.frm_tunnel_indicator,
            text="●  desconectado",
            fg=_COR_DESCONECTADO,
            font=("Segoe UI", 9, "bold"),
            bg=self.root.cget("bg"),
        )
        self.lbl_tunnel_status.pack(side="left")

        ttk.Label(topo, text="Envio automático de contadores + acesso remoto ao painel das impressoras",
                  foreground="#666").pack(anchor="w")

        # Notebook (abas)
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=6)

        self.aba_status = ttk.Frame(nb, padding=10)
        self.aba_config = ttk.Frame(nb, padding=10)
        nb.add(self.aba_status, text="  Status  ")
        nb.add(self.aba_config, text="  Configuração  ")

        self._montar_aba_status()
        self._montar_aba_config()

        # Rodapé
        rodape = ttk.Frame(self.root, padding=(14, 4, 14, 10))
        rodape.pack(fill="x", side="bottom")
        self.lbl_status_bar = ttk.Label(rodape, text="Pronto.", foreground="#333")
        self.lbl_status_bar.pack(side="left")
        ttk.Label(rodape, text=f"v{config.APP_VERSAO}",
                  foreground="#888").pack(side="right")

    def _montar_aba_status(self):
        f = self.aba_status

        box = ttk.LabelFrame(f, text="Últimas atividades", padding=10)
        box.pack(fill="x")

        self.lbl_ultima_coleta = ttk.Label(box, text="Última coleta: —")
        self.lbl_ultima_coleta.pack(anchor="w", pady=2)
        self.lbl_ultimo_envio = ttk.Label(box, text="Último envio: —")
        self.lbl_ultimo_envio.pack(anchor="w", pady=2)
        self.lbl_resultado_envio = ttk.Label(box, text="Resultado: —",
                                             foreground="#555")
        self.lbl_resultado_envio.pack(anchor="w", pady=2)

        # Status do tunnel
        box_tunnel = ttk.LabelFrame(f, text="Túnel de Acesso Remoto", padding=8)
        box_tunnel.pack(fill="x", pady=(8, 0))

        linha = ttk.Frame(box_tunnel)
        linha.pack(fill="x")
        ttk.Label(linha, text="Status:").pack(side="left")
        self.lbl_tunnel_detalhe = ttk.Label(linha, text="—", foreground="#555")
        self.lbl_tunnel_detalhe.pack(side="left", padx=(6, 0))

        ttk.Label(box_tunnel,
                  text=f"URL: {config.TUNNEL_WS_URL}",
                  foreground="#888", font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        botoes = ttk.Frame(f)
        botoes.pack(fill="x", pady=(12, 6))

        self.btn_coletar = ttk.Button(
            botoes, text="▶  Coletar e enviar agora",
            style="Accent.TButton", command=self.coletar_e_enviar_async
        )
        self.btn_coletar.pack(side="left", padx=(0, 8))

        self.btn_varrer = ttk.Button(
            botoes, text="🔍  Varrer rede (descobrir Brothers)",
            command=self.varrer_rede_async
        )
        self.btn_varrer.pack(side="left")

        self.progresso = ttk.Progressbar(f, mode="determinate")
        self.progresso.pack(fill="x", pady=(6, 8))

        box2 = ttk.LabelFrame(f, text="Impressoras detectadas", padding=6)
        box2.pack(fill="both", expand=True)

        cols = ("ip", "modelo", "serial", "contador", "origem")
        self.tree = ttk.Treeview(box2, columns=cols, show="headings", height=8)
        for c, titulo, w in [
            ("ip", "IP", 110), ("modelo", "Modelo", 160),
            ("serial", "Série", 110), ("contador", "Páginas", 90),
            ("origem", "Origem", 70),
        ]:
            self.tree.heading(c, text=titulo)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)

    def _montar_aba_config(self):
        f = self.aba_config

        canvas = tk.Canvas(f, highlightthickness=0)
        scrollbar = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        container = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=container, anchor="nw")
        container.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # Nome do agente
        grp1 = ttk.LabelFrame(container, text="Identificação deste computador", padding=10)
        grp1.pack(fill="x", pady=(0, 10))
        ttk.Label(grp1, text="Nome do agente (aparece no Sigatec):").grid(row=0, column=0, sticky="w")
        self.var_agente = tk.StringVar(value=self.cfg.get("nome_agente", ""))
        ttk.Entry(grp1, textvariable=self.var_agente, width=40).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        grp1.columnconfigure(1, weight=1)

        # Tunnel
        grp_tunnel = ttk.LabelFrame(container, text="Acesso Remoto (Tunnel WebSocket)", padding=10)
        grp_tunnel.pack(fill="x", pady=(0, 10))

        self.var_tunnel = tk.BooleanVar(value=self.cfg.get("tunnel_ativo", True))
        ttk.Checkbutton(
            grp_tunnel,
            text="Ativar acesso remoto ao painel web das impressoras",
            variable=self.var_tunnel
        ).pack(anchor="w")
        ttk.Label(grp_tunnel,
                  text=f"Servidor: {config.TUNNEL_WS_URL}",
                  foreground="#888", font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        # Envio automático
        grp_master = ttk.LabelFrame(container, text="Envio automático", padding=10)
        grp_master.pack(fill="x", pady=(0, 10))

        self.var_auto = tk.BooleanVar(value=self.cfg.get("envio_automatico", True))
        ttk.Checkbutton(
            grp_master,
            text="Ativar envio automático (desmarque para coletar apenas manualmente)",
            variable=self.var_auto
        ).pack(anchor="w")

        self.lbl_proxima = ttk.Label(grp_master, text="Próxima execução: —",
                                     foreground="#0070C0")
        self.lbl_proxima.pack(anchor="w", pady=(6, 0))

        ag = self.cfg.get("agendamento", {}) or {}

        # Modo Diário
        grp_d = ttk.LabelFrame(container, text="Modo Diário", padding=10)
        grp_d.pack(fill="x", pady=(0, 10))

        d_cfg = ag.get("diario", {}) or {}
        self.var_d_ativo = tk.BooleanVar(value=d_cfg.get("ativo", True))
        self.var_d_hora = tk.StringVar(value=d_cfg.get("horario", "18:00"))

        ttk.Checkbutton(grp_d, text="Ativar modo diário",
                        variable=self.var_d_ativo).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(grp_d, text="Horário:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(grp_d, textvariable=self.var_d_hora, width=8).grid(
            row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Label(grp_d, text=" (HH:MM, 24h)",
                  foreground="#666").grid(row=1, column=2, sticky="w", pady=(6, 0))

        # Modo Semanal
        grp_s = ttk.LabelFrame(container, text="Modo Semanal", padding=10)
        grp_s.pack(fill="x", pady=(0, 10))

        s_cfg = ag.get("semanal", {}) or {}
        self.var_s_ativo = tk.BooleanVar(value=s_cfg.get("ativo", False))
        self.var_s_hora = tk.StringVar(value=s_cfg.get("horario", "18:00"))

        ttk.Checkbutton(grp_s, text="Ativar modo semanal",
                        variable=self.var_s_ativo).grid(row=0, column=0, columnspan=8, sticky="w")

        ttk.Label(grp_s, text="Dias da semana:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._dias_semana_map = [
            ("Seg", 0), ("Ter", 1), ("Qua", 2), ("Qui", 3),
            ("Sex", 4), ("Sáb", 5), ("Dom", 6),
        ]
        dias_ativos = set(s_cfg.get("dias", []) or [])
        self.vars_s_dias = {}
        for i, (nome, idx) in enumerate(self._dias_semana_map):
            v = tk.BooleanVar(value=(idx in dias_ativos))
            self.vars_s_dias[idx] = v
            ttk.Checkbutton(grp_s, text=nome, variable=v).grid(
                row=1, column=1 + i, sticky="w", padx=2, pady=(6, 0))

        ttk.Label(grp_s, text="Horário:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(grp_s, textvariable=self.var_s_hora, width=8).grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Label(grp_s, text=" (HH:MM, 24h)",
                  foreground="#666").grid(row=2, column=2, columnspan=3,
                                          sticky="w", pady=(6, 0))

        # Modo Mensal
        grp_m = ttk.LabelFrame(container, text="Modo Mensal (até 3 dias)", padding=10)
        grp_m.pack(fill="x", pady=(0, 10))

        m_cfg = ag.get("mensal", {}) or {}
        self.var_m_ativo = tk.BooleanVar(value=m_cfg.get("ativo", False))
        ttk.Checkbutton(grp_m, text="Ativar modo mensal",
                        variable=self.var_m_ativo).grid(row=0, column=0, columnspan=5, sticky="w")

        self.vars_m_linhas = []
        dias_existentes = list(m_cfg.get("dias", []) or [])
        for i in range(3):
            dia_val = ""
            hora_val = ""
            if i < len(dias_existentes):
                try:
                    dia_val = str(dias_existentes[i].get("dia", "") or "")
                except Exception:
                    dia_val = ""
                hora_val = (dias_existentes[i].get("horario") or "") if isinstance(dias_existentes[i], dict) else ""

            v_dia = tk.StringVar(value=dia_val)
            v_hora = tk.StringVar(value=hora_val)
            self.vars_m_linhas.append((v_dia, v_hora))

            ttk.Label(grp_m, text=f"Dia {i+1}:").grid(
                row=1 + i, column=0, sticky="w", pady=(6, 0))
            ttk.Entry(grp_m, textvariable=v_dia, width=4).grid(
                row=1 + i, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
            ttk.Label(grp_m, text="às").grid(
                row=1 + i, column=2, padx=(6, 0), pady=(6, 0))
            ttk.Entry(grp_m, textvariable=v_hora, width=8).grid(
                row=1 + i, column=3, sticky="w", padx=(6, 0), pady=(6, 0))
            ttk.Label(grp_m, text=" (ex: dia 15 às 18:00)",
                      foreground="#666").grid(
                row=1 + i, column=4, sticky="w", padx=(6, 0), pady=(6, 0))

        ttk.Label(grp_m,
                  text="Se o dia não existe no mês (ex: 31 em fev), roda no último dia do mês.",
                  foreground="#666").grid(row=4, column=0, columnspan=5,
                                          sticky="w", pady=(8, 0))

        # Inicialização com Windows
        grp3 = ttk.LabelFrame(container, text="Inicialização com o Windows", padding=10)
        grp3.pack(fill="x", pady=(0, 10))
        self.var_autostart = tk.BooleanVar(value=self.cfg.get("iniciar_com_windows", True))
        ttk.Checkbutton(grp3, text="Iniciar com o Windows (recomendado)",
                        variable=self.var_autostart).pack(anchor="w")

        # Botões
        barra = ttk.Frame(container)
        barra.pack(fill="x", pady=(10, 10))
        ttk.Button(barra, text="Testar conexão com Sigatec",
                   command=self.testar_conexao_async).pack(side="left")
        ttk.Button(barra, text="Salvar configurações",
                   style="Accent.TButton",
                   command=self.salvar_configuracoes).pack(side="right")

        self._atualizar_proxima_execucao()

    # ─── Atualizações de UI ──────────────────────────────────────────────────

    def _atualizar_status(self):
        uc = self.cfg.get("ultima_coleta")
        ue = self.cfg.get("ultimo_envio_ok")
        st = self.cfg.get("ultimo_envio_status") or "—"
        self.lbl_ultima_coleta.config(text=f"Última coleta:  {uc or '—'}")
        self.lbl_ultimo_envio.config(text=f"Último envio OK:  {ue or '—'}")
        self.lbl_resultado_envio.config(text=f"Resultado:  {st}")

    def _atualizar_indicador_tunnel(self):
        """Atualiza os labels de status do túnel na UI."""
        status = _tunnel_mod.get_status()
        cor = {
            "conectado":    _COR_CONECTADO,
            "reconectando": _COR_RECONECTANDO,
        }.get(status, _COR_DESCONECTADO)

        texto = f"●  {status}"
        self.lbl_tunnel_status.config(text=texto, fg=cor)
        if hasattr(self, "lbl_tunnel_detalhe"):
            self.lbl_tunnel_detalhe.config(
                text=status.capitalize(),
                foreground=cor,
            )

    def _set_status_bar(self, texto: str):
        self.lbl_status_bar.config(text=texto)

    def _processar_fila(self):
        try:
            while True:
                msg = self.fila_ui.get_nowait()
                tipo = msg.get("tipo")
                if tipo == "status":
                    self._set_status_bar(msg["texto"])
                elif tipo == "progresso":
                    self.progresso["value"] = msg["pct"]
                elif tipo == "limpar_tree":
                    self.tree.delete(*self.tree.get_children())
                elif tipo == "add_linha":
                    self.tree.insert("", "end", values=msg["valores"])
                elif tipo == "habilitar_botoes":
                    self.btn_coletar.config(state="normal")
                    self.btn_varrer.config(state="normal")
                elif tipo == "desabilitar_botoes":
                    self.btn_coletar.config(state="disabled")
                    self.btn_varrer.config(state="disabled")
                elif tipo == "refresh_status":
                    self.cfg = carregar_config()
                    self._atualizar_status()
                elif tipo == "messagebox":
                    fn = getattr(messagebox, msg["kind"])
                    fn(msg["titulo"], msg["mensagem"])
        except queue.Empty:
            pass
        self.root.after(150, self._processar_fila)

    # ─── Ações ───────────────────────────────────────────────────────────────

    def salvar_configuracoes(self):
        def _valida_hora(texto: str) -> bool:
            try:
                hh, mm = texto.split(":")
                return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
            except Exception:
                return False

        erros = []

        d_hora = self.var_d_hora.get().strip()
        if self.var_d_ativo.get() and not _valida_hora(d_hora):
            erros.append(f"• Horário do modo Diário inválido: '{d_hora}' (use HH:MM)")

        s_hora = self.var_s_hora.get().strip()
        s_dias = [idx for idx, v in self.vars_s_dias.items() if v.get()]
        if self.var_s_ativo.get():
            if not _valida_hora(s_hora):
                erros.append(f"• Horário do modo Semanal inválido: '{s_hora}' (use HH:MM)")
            if not s_dias:
                erros.append("• Modo Semanal ativo mas nenhum dia da semana marcado.")

        mensal_entries = []
        for i, (v_dia, v_hora) in enumerate(self.vars_m_linhas, start=1):
            raw_dia = v_dia.get().strip()
            raw_hora = v_hora.get().strip()
            if not raw_dia and not raw_hora:
                continue
            try:
                n_dia = int(raw_dia)
            except Exception:
                if self.var_m_ativo.get():
                    erros.append(f"• Mensal linha {i}: dia '{raw_dia}' não é um número válido.")
                continue
            if not (1 <= n_dia <= 31):
                if self.var_m_ativo.get():
                    erros.append(f"• Mensal linha {i}: dia {n_dia} fora do intervalo 1–31.")
                continue
            if not _valida_hora(raw_hora):
                if self.var_m_ativo.get():
                    erros.append(f"• Mensal linha {i}: horário '{raw_hora}' inválido (use HH:MM).")
                continue
            mensal_entries.append({"dia": n_dia, "horario": raw_hora})

        if self.var_m_ativo.get() and not mensal_entries:
            erros.append("• Modo Mensal ativo mas nenhum dia preenchido corretamente.")

        if (self.var_auto.get()
                and not self.var_d_ativo.get()
                and not self.var_s_ativo.get()
                and not self.var_m_ativo.get()):
            erros.append("• Envio automático está ativado mas nenhum modo "
                         "(Diário/Semanal/Mensal) foi escolhido.")

        if erros:
            messagebox.showerror("Configurações com erro",
                                 "Corrija os seguintes pontos:\n\n" + "\n".join(erros))
            return

        # Persiste
        self.cfg["nome_agente"] = self.var_agente.get().strip() or "PC"
        self.cfg["envio_automatico"] = bool(self.var_auto.get())
        self.cfg["iniciar_com_windows"] = bool(self.var_autostart.get())
        self.cfg["tunnel_ativo"] = bool(self.var_tunnel.get())
        self.cfg["agendamento"] = {
            "diario": {
                "ativo": bool(self.var_d_ativo.get()),
                "horario": d_hora or "18:00",
            },
            "semanal": {
                "ativo": bool(self.var_s_ativo.get()),
                "dias": sorted(s_dias),
                "horario": s_hora or "18:00",
            },
            "mensal": {
                "ativo": bool(self.var_m_ativo.get()),
                "dias": mensal_entries,
            },
        }
        self.cfg.pop("horario_envio", None)

        salvar_config(self.cfg)

        if self.cfg["iniciar_com_windows"]:
            windows_startup.cadastrar_autoinicio()
        else:
            windows_startup.descadastrar_autoinicio()

        # Aplica mudança de tunnel em tempo real
        if self.cfg["tunnel_ativo"]:
            _tunnel_mod.iniciar_tunnel()
        else:
            _tunnel_mod.parar_tunnel()

        self._atualizar_proxima_execucao()
        messagebox.showinfo("Configurações salvas", "Ajustes gravados com sucesso.")

    def _atualizar_proxima_execucao(self):
        try:
            dt = _proxima_execucao()
            if not dt:
                self.lbl_proxima.config(
                    text="Próxima execução: — (envio automático desligado ou sem modos)")
                return
            dias_pt = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
            txt = (f"Próxima execução: {dias_pt[dt.weekday()]} "
                   f"{dt.strftime('%d/%m/%Y')} às {dt.strftime('%H:%M')}")
            self.lbl_proxima.config(text=txt)
        except Exception as e:
            log.debug("Erro atualizando próxima execução: %s", e)

    def testar_conexao_async(self):
        def worker():
            self.fila_ui.put({"tipo": "status", "texto": "Testando conexão..."})
            ok, msg = testar_conexao()
            kind = "showinfo" if ok else "showerror"
            titulo = "Conexão OK" if ok else "Falha de conexão"
            self.fila_ui.put({"tipo": "messagebox", "kind": kind,
                              "titulo": titulo, "mensagem": msg})
            self.fila_ui.put({"tipo": "status", "texto": "Pronto."})
        threading.Thread(target=worker, daemon=True).start()

    def varrer_rede_async(self):
        def worker():
            self.fila_ui.put({"tipo": "desabilitar_botoes"})
            self.fila_ui.put({"tipo": "limpar_tree"})

            self.fila_ui.put({"tipo": "status", "texto": "Lendo USB local..."})
            leituras_total = []
            try:
                leituras_usb = coletar_usb()
                leituras_total.extend(leituras_usb)
                for l in leituras_usb:
                    self.fila_ui.put({"tipo": "add_linha", "valores": (
                        l.ip, l.modelo, l.serial,
                        f"{l.contagem_paginas:,}".replace(",", "."),
                        l.origem
                    )})
            except Exception as e:
                log.error("Erro lendo USB durante varredura: %s", e)

            self.fila_ui.put({"tipo": "status", "texto": "Varrendo rede..."})

            def progresso(atual, total, ip):
                pct = (atual / max(1, total)) * 100
                self.fila_ui.put({"tipo": "progresso", "pct": pct})

            ips = varrer_rede(callback_progresso=progresso)
            self.fila_ui.put({"tipo": "progresso", "pct": 0})

            if not ips and not leituras_total:
                self.fila_ui.put({"tipo": "messagebox", "kind": "showwarning",
                                  "titulo": "Nenhuma Brother encontrada",
                                  "mensagem": "A varredura não encontrou impressoras Brother (nem USB, nem na rede)."})
            elif ips:
                self.cfg["ips_conhecidos"] = ips
                salvar_config(self.cfg)

                self.fila_ui.put({"tipo": "status",
                                  "texto": f"Lendo {len(ips)} impressora(s) de rede..."})
                leituras_rede = coletar_de_ips(ips, callback_progresso=progresso)
                leituras_total.extend(leituras_rede)
                for l in leituras_rede:
                    self.fila_ui.put({"tipo": "add_linha", "valores": (
                        l.ip, l.modelo, l.serial,
                        f"{l.contagem_paginas:,}".replace(",", "."),
                        l.origem
                    )})

            self.fila_ui.put({"tipo": "progresso", "pct": 0})
            self.fila_ui.put({"tipo": "status",
                              "texto": f"Varredura concluída. {len(leituras_total)} impressora(s) detectada(s)."})
            self.fila_ui.put({"tipo": "habilitar_botoes"})

        threading.Thread(target=worker, daemon=True).start()

    def coletar_e_enviar_async(self):
        def worker():
            self.fila_ui.put({"tipo": "desabilitar_botoes"})
            self.fila_ui.put({"tipo": "limpar_tree"})
            self.fila_ui.put({"tipo": "status", "texto": "Coletando..."})

            leituras = []

            try:
                leituras_usb = coletar_usb()
                leituras.extend(leituras_usb)
            except Exception as e:
                log.error("Erro USB: %s", e)

            cfg = carregar_config()
            ips = cfg.get("ips_conhecidos") or []
            if not ips:
                self.fila_ui.put({"tipo": "status",
                                  "texto": "Nenhum IP salvo — varrendo rede..."})
                ips = varrer_rede()
                if ips:
                    cfg["ips_conhecidos"] = ips
                    salvar_config(cfg)

            if ips:
                self.fila_ui.put({"tipo": "status",
                                  "texto": f"Lendo {len(ips)} impressora(s) de rede..."})
                leituras.extend(coletar_de_ips(ips))

            for l in leituras:
                self.fila_ui.put({"tipo": "add_linha", "valores": (
                    l.ip, l.modelo, l.serial,
                    f"{l.contagem_paginas:,}".replace(",", "."),
                    l.origem
                )})

            if not leituras:
                self.fila_ui.put({"tipo": "messagebox", "kind": "showwarning",
                                  "titulo": "Nada para enviar",
                                  "mensagem": "Nenhuma impressora Brother foi lida."})
            else:
                self.fila_ui.put({"tipo": "status", "texto": "Enviando ao Sigatec..."})
                try:
                    resp = enviar_leituras(leituras)
                    validas = [l for l in leituras if l.valida()]
                    novos = resp.get("novos", 0)
                    total = resp.get("total", 0)
                    duplicadas = max(0, len(validas) - novos)
                    self.fila_ui.put({"tipo": "messagebox", "kind": "showinfo",
                                      "titulo": "Envio concluído",
                                      "mensagem": (
                                          f"Leituras enviadas: {len(validas)}\n"
                                          f"Novas gravadas: {novos}\n"
                                          f"Duplicadas (já existiam): {duplicadas}\n"
                                          f"\nTotal no sistema: {total}"
                                      )})
                except SigatecAPIError as e:
                    self.fila_ui.put({"tipo": "messagebox", "kind": "showerror",
                                      "titulo": "Falha no envio",
                                      "mensagem": str(e)})

            self.fila_ui.put({"tipo": "status", "texto": "Pronto."})
            self.fila_ui.put({"tipo": "refresh_status"})
            self.fila_ui.put({"tipo": "habilitar_botoes"})

        threading.Thread(target=worker, daemon=True).start()

    # ─── Bandeja ─────────────────────────────────────────────────────────────

    def minimizar_para_bandeja(self):
        self.root.withdraw()
        log.info("Janela minimizada para bandeja")

    def mostrar_janela(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def encerrar(self):
        log.info("Encerrando aplicativo")
        try:
            self.agendador.parar()
            _tunnel_mod.parar_tunnel()
            self.bandeja.parar()
        finally:
            self.root.quit()
            self.root.destroy()
            sys.exit(0)

    # ─── Loop principal ──────────────────────────────────────────────────────

    def rodar(self):
        self.root.mainloop()
