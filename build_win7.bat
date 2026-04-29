@echo off
REM ============================================================
REM  Build Win 7 do Sigatec Coletor Pro
REM
REM  Le a SIGATEC_INGEST_KEY do .env automaticamente.
REM  Gera dist/SigatecColetorPro_Win7_<data>.exe
REM
REM  Pre-requisito: Python 3.8 instalado (para compatibilidade com Win 7)
REM                 + pyinstaller + pyinstaller-hooks-contrib
REM                 + requirements.txt instalado (pip install -r requirements.txt)
REM ============================================================

cd /d "%~dp0"

REM Le a chave do .env
set INGEST_KEY=
for /f "tokens=2 delims==" %%a in ('findstr /B "SIGATEC_INGEST_KEY=" .env 2^>nul') do set INGEST_KEY=%%a

if "%INGEST_KEY%"=="" (
    echo ============================================================
    echo  ERRO: SIGATEC_INGEST_KEY nao encontrada no arquivo .env
    echo  Verifique se .env existe em "%~dp0" e tem a linha:
    echo      SIGATEC_INGEST_KEY=...
    echo ============================================================
    pause
    exit /b 1
)

echo ============================================================
echo  Build Sigatec Coletor Pro - Win 7
echo ============================================================
echo  Chave INGEST: %INGEST_KEY:~0,8%... (primeiros 8 chars)
echo  Versao Python:
python --version
echo ============================================================
echo.

py -3.8 installer\build.py --ingest-key "%INGEST_KEY%" --suffix Win7 --clean

if errorlevel 1 (
    echo.
    echo ============================================================
    echo  Build FALHOU.
    echo ============================================================
) else (
    echo.
    echo ============================================================
    echo  Build CONCLUIDO. Veja o arquivo gerado em dist\
    echo ============================================================
)
pause
