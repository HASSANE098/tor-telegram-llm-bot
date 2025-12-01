import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# --- Настройки бота ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN не установлен в .env файле!")

# --- Настройки Ollama ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen2.5:7b-instruct-q4_K_M")  # Qwen - лучшая для русского!
DEEP_MODEL = os.getenv("DEEP_MODEL", "mistral:7b-instruct-q4_K_M")        # Mistral для Deep режима

# --- Таймауты ---
# Увеличены для медленных систем и Deep режима
MAX_STREAM_TIMEOUT = int(os.getenv("MAX_STREAM_TIMEOUT", "600"))  # 10 минут для обычного режима
# Deep режим автоматически использует MAX_STREAM_TIMEOUT * 2 = 20 минут

# --- Настройки Базы Данных ---
DB_PATH = os.getenv("DB_PATH", "supreme_cache.db")