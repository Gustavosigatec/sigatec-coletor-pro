---
name: sigatec-coletor-pro
description: Skill para desenvolvimento do Sigatec Coletor Pro - agente Windows com coleta de contadores + acesso remoto a impressoras Brother via túnel WebSocket
---

# Sigatec Coletor Pro — Skill de Desenvolvimento

## Contexto
Leia CLAUDE.md nesta pasta antes de qualquer trabalho.

## Projetos relacionados (ler quando necessário)
- C:\sigatec-coletor\sigatec-coletor — base de código original (SOMENTE LEITURA, não alterar)
- C:\brother-counter — sistema de gestão central (alterar quando necessário pra integração)

## Ao iniciar qualquer tarefa
1. Leia CLAUDE.md desta pasta
2. Se precisar entender o coletor original: leia C:\sigatec-coletor\sigatec-coletor\docs\ARQUITETURA.md
3. Se precisar entender o brother-counter: leia C:\brother-counter\cgibase-api\main.py (endpoints)

## Regras
- NUNCA alterar o sigatec-coletor original
- O Coletor Pro é um fork independente — copiar código, não importar
- Toda integração com brother-counter deve usar os endpoints existentes ou criar novos no padrão /api/bc/
- Autenticação via X-API-Key + X-Installation-ID (nunca OAuth ou JWT para o agente)
- Python puro, sem frameworks pesados no agente
- O exe final deve ser leve (~25-35 MB via PyInstaller)
- Rodar silencioso na bandeja, sem janelas desnecessárias
- Varredura de rede deve detectar mudanças de IP automaticamente

## Ordem de implementação recomendada
1. Fork do sigatec-coletor → pasta do projeto
2. Módulo tunnel.py (WebSocket client no agente)
3. Router tunnel no brother-counter (WebSocket server + HTTP proxy)
4. Frontend: botão "Acessar painel" no brother-counter
5. Testes de proxy com impressora real
6. Build PyInstaller do Pro
7. Documentação

## Técnicos de campo
Mateus e Gabriel (referência para estoque de peças na bancada do brother-counter)
