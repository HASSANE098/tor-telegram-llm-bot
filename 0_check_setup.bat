@echo off
chcp 65001 >nul
title Проверка окружения ТОРа
cd /d "%~dp0"

echo ======================================================
echo   🔍 ПРОВЕРКА ОКРУЖЕНИЯ ТОРа
echo ======================================================
echo.

REM Проверяем Python
echo [1/6] Проверка Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python не найден!
    echo 💡 Установите Python 3.8+ с python.org
    goto :error
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VER=%%i
echo ✅ Python %PYTHON_VER%
echo.

REM Проверяем виртуальное окружение
echo [2/6] Проверка виртуального окружения...
if exist "venv\" (
    echo ✅ Виртуальное окружение найдено
) else (
    echo ⚠️  Виртуальное окружение не найдено
    echo 💡 Оно будет создано при первом запуске
)
echo.

REM Проверяем .env
echo [3/6] Проверка файла конфигурации...
if exist ".env" (
    echo ✅ Файл .env найден
    findstr /C:"TELEGRAM_BOT_TOKEN=ВАШ" .env >nul
    if not errorlevel 1 (
        echo ⚠️  TELEGRAM_BOT_TOKEN не настроен!
        echo 💡 Отредактируйте .env и добавьте токен от @BotFather
    )
) else (
    echo ⚠️  Файл .env не найден
    echo 💡 Он будет создан при первом запуске
)
echo.

REM Проверяем Ollama
echo [4/6] Проверка Ollama...
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo ❌ Ollama не запущена!
    echo 💡 Запустите: ollama serve
    echo 💡 Или установите с ollama.ai
    set OLLAMA_OK=0
) else (
    echo ✅ Ollama запущена
    set OLLAMA_OK=1
)
echo.

REM Проверяем модели Ollama
if %OLLAMA_OK%==1 (
    echo [5/6] Проверка моделей Ollama...
    curl -s http://localhost:11434/api/tags > temp_models.json
    findstr /C:"qwen2.5" temp_models.json >nul
    if errorlevel 1 (
        echo ⚠️  Модель qwen2.5 не найдена
        echo 💡 Установите: ollama pull qwen2.5:7b-instruct-q4_K_M
    ) else (
        echo ✅ qwen2.5 установлена
    )
    
    findstr /C:"mistral" temp_models.json >nul
    if errorlevel 1 (
        echo ⚠️  Модель mistral не найдена
        echo 💡 Установите: ollama pull mistral:7b-instruct-q4_K_M
    ) else (
        echo ✅ mistral установлена
    )
    del temp_models.json >nul 2>&1
) else (
    echo [5/6] Пропущено (Ollama не запущена)
)
echo.

REM Проверяем зависимости Python
echo [6/6] Проверка Python пакетов...
if exist "venv\" (
    call venv\Scripts\activate
    
    python -c "import aiogram" >nul 2>&1
    if errorlevel 1 (
        echo ⚠️  aiogram не установлен
    ) else (
        echo ✅ aiogram
    )
    
    python -c "import langchain" >nul 2>&1
    if errorlevel 1 (
        echo ⚠️  langchain не установлен
    ) else (
        echo ✅ langchain
    )
    
    python -c "import chromadb" >nul 2>&1
    if errorlevel 1 (
        echo ⚠️  chromadb не установлен
    ) else (
        echo ✅ chromadb
    )
) else (
    echo ⚠️  Виртуальное окружение не создано
    echo 💡 Запустите 1_run.bat для установки
)
echo.

echo ======================================================
echo   📋 ИТОГИ ПРОВЕРКИ
echo ======================================================
echo.
echo ✅ - Всё в порядке
echo ⚠️  - Требует внимания
echo ❌ - Критическая ошибка
echo.
echo 💡 СЛЕДУЮЩИЕ ШАГИ:
echo    1. Установите Ollama (если ещё нет)
echo    2. Запустите: ollama serve
echo    3. Скачайте модели командами выше
echo    4. Настройте .env файл (токен бота)
echo    5. Запустите: 1_run.bat
echo.
echo ======================================================
pause
goto :eof

:error
echo.
echo ❌ Критическая ошибка!
echo.
pause
exit /b 1