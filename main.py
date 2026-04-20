"""
Sigatec Coletor Pro — ponto de entrada principal.

Uso:
    python main.py                   # abre janela
    python main.py --minimizado      # abre direto na bandeja (autostart)
    python main.py --coletar         # executa uma coleta e sai (modo headless)
    python main.py --testar-conexao  # testa conexão e sai
    python main.py --diagnostico-usb # imprime tudo que o Windows sabe sobre impressoras USB
"""
import sys
from coletor.utils import get_logger

log = get_logger()


def main():
    args = sys.argv[1:]

    # Modo headless: coleta e sai (útil para rodar via Agendador de Tarefas)
    if "--coletar" in args:
        from coletor.snmp_reader import varrer_rede, coletar_de_ips
        from coletor.usb_reader import coletar_usb
        from coletor.api_client import enviar_leituras, SigatecAPIError
        from coletor.utils import carregar_config, salvar_config

        log.info("Modo headless: coletando e enviando")
        leituras = []
        try:
            leituras.extend(coletar_usb())
        except Exception as e:
            log.error("Erro USB: %s", e)

        cfg = carregar_config()
        ips = cfg.get("ips_conhecidos") or []
        if not ips:
            ips = varrer_rede()
            if ips:
                cfg["ips_conhecidos"] = ips
                salvar_config(cfg)
        leituras.extend(coletar_de_ips(ips))

        if not leituras:
            log.warning("Nenhuma leitura — encerrando")
            sys.exit(2)

        try:
            resp = enviar_leituras(leituras)
            log.info("Envio OK: %s", resp)
            sys.exit(0)
        except SigatecAPIError as e:
            log.error("Falha no envio: %s", e)
            sys.exit(1)

    # Teste de conexão
    if "--testar-conexao" in args:
        from coletor.api_client import testar_conexao
        ok, msg = testar_conexao()
        print(msg)
        sys.exit(0 if ok else 1)

    # Diagnóstico USB (mostra no console tudo que o Windows vê de impressora)
    if "--diagnostico-usb" in args:
        from coletor.usb_reader import imprimir_diagnostico_usb
        imprimir_diagnostico_usb()
        sys.exit(0)

    # Modo padrão: interface gráfica
    from coletor.ui import AppColetor
    app = AppColetor(iniciar_minimizado="--minimizado" in args)
    app.rodar()


if __name__ == "__main__":
    main()
