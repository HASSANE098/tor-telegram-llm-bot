# -*- coding: utf-8 -*-
"""
ТОР (TOR) - Творческий Олимпийский Разум
Telegram AI Bot на базе Ollama LLM

Версия 2.2 - Умная работа в групповых чатах
- smart режим по умолчанию
- /chat_mode для настройки режима чата
- Коррекция RAG для русского языка
"""

import logging
import hashlib
import json
import aiosqlite 
import requests
import asyncio
import os
import re
from datetime import datetime
from collections import deque
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command 
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup 
from aiogram.exceptions import TelegramBadRequest
from config import (
    TELEGRAM_BOT_TOKEN, OLLAMA_URL, DEFAULT_MODEL,
    DEEP_MODEL, DB_PATH, MAX_STREAM_TIMEOUT
)
from rag_manager import rag_manager

# === КОНФИГУРАЦИЯ ===
CONTEXT_WINDOW = 10
MAX_TELEGRAM_LENGTH = 4096
CURRENT_TEMPERATURE = 0.8
RAG_ENABLED = False  # Будет включено после инициализации

# === НАСТРОЙКИ ОЧЕРЕДЕЙ ===
MAX_CONCURRENT_REQUESTS = 1  # ВАЖНО: 1 для CPU! (последовательная обработка)
MAX_QUEUE_SIZE = 10          # Максимальный размер очереди
REQUEST_TIMEOUT = 600        # Таймаут на запрос (10 минут)

# === НАСТРОЙКИ ДЛЯ ГРУПП ===
DEFAULT_GROUP_MODE = "smart"  # Режим по умолчанию: smart, mention, all, off
GROUP_ADMIN_ONLY_COMMANDS = ["clear", "temp", "stats", "rag_init", "rag_clear", "chat_mode"]

# === РЕЖИМЫ ЧАТОВ ===
CHAT_MODES = {
    "smart": "🧠 Умный — отвечает на упоминания, ответы и вопросы в воздух",
    "mention": "📢 Упоминания — только @бот и ответы на сообщения бота",
    "all": "💬 Всё — отвечает на каждое сообщение",
    "off": "🔇 Выключен — только команды"
}

# === ДОСТУПНЫЕ МОДЕЛИ ===
AVAILABLE_MODELS = [
    "qwen2.5:7b-instruct-q4_K_M",
    "mistral:7b-instruct-q4_K_M"
]

# Создаём папку data для всех данных
os.makedirs("./data", exist_ok=True)

# === Логирование ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("supreme")

# === Инициализация ===
bot = Bot(token=TELEGRAM_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
db_conn = None

# === Кэш режимов чатов (в памяти) ===
chat_modes_cache = {}

# === FSM States ===
class BotStates(StatesGroup):
    deep_mode = State()

# ============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ HTML
# ============================================

def escape_html(text: str) -> str:
    """Экранирует специальные символы HTML для безопасного вывода"""
    if not text:
        return ""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))

# ============================================
# СИСТЕМА ОЧЕРЕДЕЙ
# ============================================

class RequestQueue:
    """Управление очередью запросов к LLM"""
    
    def __init__(self, max_concurrent: int = 2, max_queue_size: int = 10):
        self.max_concurrent = max_concurrent
        self.max_queue_size = max_queue_size
        self.active_requests = 0
        self.queue = deque()
        self.lock = asyncio.Lock()
        self.queue_stats = {
            'total_processed': 0,
            'total_queued': 0,
            'total_rejected': 0,
            'avg_wait_time': 0
        }
    
    async def can_process(self) -> bool:
        """Проверяет, можно ли обработать запрос сейчас"""
        async with self.lock:
            return self.active_requests < self.max_concurrent
    
    async def add_to_queue(self, request_data: dict) -> int:
        """Добавляет запрос в очередь, возвращает позицию"""
        async with self.lock:
            if len(self.queue) >= self.max_queue_size:
                self.queue_stats['total_rejected'] += 1
                return -1  # Очередь переполнена
            
            request_data['queued_at'] = datetime.now()
            self.queue.append(request_data)
            self.queue_stats['total_queued'] += 1
            position = len(self.queue)
            logger.info(f"📋 Request added to queue. Position: {position}, Queue size: {len(self.queue)}")
            return position
    
    async def start_processing(self):
        """Отмечает начало обработки запроса"""
        async with self.lock:
            self.active_requests += 1
            logger.info(f"🔄 Active requests: {self.active_requests}/{self.max_concurrent}")
    
    async def finish_processing(self):
        """Отмечает завершение обработки запроса"""
        async with self.lock:
            self.active_requests = max(0, self.active_requests - 1)
            self.queue_stats['total_processed'] += 1
            logger.info(f"✅ Request finished. Active requests: {self.active_requests}/{self.max_concurrent}")
    
    async def get_next_request(self):
        """Получает следующий запрос из очереди"""
        async with self.lock:
            if self.queue:
                request = self.queue.popleft()
                wait_time = (datetime.now() - request['queued_at']).total_seconds()
                
                # Обновляем среднее время ожидания
                total = self.queue_stats['total_processed']
                if total > 0:
                    avg = self.queue_stats['avg_wait_time']
                    self.queue_stats['avg_wait_time'] = (avg * total + wait_time) / (total + 1)
                else:
                    self.queue_stats['avg_wait_time'] = wait_time
                
                logger.info(f"⏱️ Request waited {wait_time:.1f}s in queue")
                return request
            return None
    
    async def get_queue_info(self) -> dict:
        """Возвращает информацию о состоянии очереди"""
        async with self.lock:
            return {
                'active': self.active_requests,
                'queued': len(self.queue),
                'max_concurrent': self.max_concurrent,
                'stats': self.queue_stats.copy()
            }

# Глобальная очередь
request_queue = RequestQueue(max_concurrent=MAX_CONCURRENT_REQUESTS, max_queue_size=MAX_QUEUE_SIZE)

# Обработчик очереди (запускается в фоне)
async def queue_processor():
    """Фоновый процесс обработки очереди"""
    logger.info("🔄 Queue processor started")
    
    while True:
        try:
            # Проверяем, можем ли обработать новый запрос
            if await request_queue.can_process():
                request_data = await request_queue.get_next_request()
                
                if request_data:
                    # Обрабатываем запрос асинхронно
                    asyncio.create_task(process_queued_request(request_data))
            
            # Небольшая пауза перед следующей проверкой
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.exception(f"Error in queue processor: {e}")
            await asyncio.sleep(1)

async def process_queued_request(request_data: dict):
    """Обрабатывает запрос из очереди"""
    await request_queue.start_processing()
    
    try:
        await process_message(
            request_data['message'],
            request_data['model'],
            request_data['is_deep']
        )
    except Exception as e:
        logger.exception(f"Error processing queued request: {e}")
        try:
            await request_data['message'].reply(f"❌ Ошибка обработки запроса: {escape_html(str(e))}", parse_mode="HTML")
        except:
            pass
    finally:
        await request_queue.finish_processing()

# ============================================
# ФУНКЦИИ РАБОТЫ С ГРУППАМИ
# ============================================

async def is_group_chat(message: types.Message) -> bool:
    """Проверяет, является ли чат групповым."""
    return message.chat.type in ["group", "supergroup"]

async def is_user_admin(message: types.Message) -> bool:
    """Проверяет, является ли пользователь администратором группы."""
    if not await is_group_chat(message):
        return True
    
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ["creator", "administrator"]
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

def is_bot_mentioned(message: types.Message) -> bool:
    """Проверяет, упомянут ли бот в сообщении через @username."""
    if not message.text:
        return False
    
    if message.entities:
        for entity in message.entities:
            if entity.type == "mention":
                mention = message.text[entity.offset:entity.offset + entity.length]
                bot_username = bot._me.username if hasattr(bot, '_me') and bot._me else None
                if bot_username and mention.lower() == f"@{bot_username.lower()}":
                    return True
    
    return False

def is_reply_to_bot(message: types.Message) -> bool:
    """Проверяет, является ли сообщение ответом на сообщение бота."""
    if not message.reply_to_message:
        return False
    
    if not message.reply_to_message.from_user:
        return False
    
    if not message.reply_to_message.from_user.is_bot:
        return False
    
    bot_id = bot._me.id if hasattr(bot, '_me') and bot._me else None
    if bot_id and message.reply_to_message.from_user.id == bot_id:
        return True
    
    return False

def has_other_mentions(message: types.Message) -> bool:
    """Проверяет, есть ли упоминания других пользователей (не бота)."""
    if not message.text or not message.entities:
        return False
    
    bot_username = bot._me.username.lower() if hasattr(bot, '_me') and bot._me else None
    
    for entity in message.entities:
        if entity.type == "mention":
            mention = message.text[entity.offset:entity.offset + entity.length].lower()
            # Если это не упоминание нашего бота — значит упомянут кто-то другой
            if bot_username and mention != f"@{bot_username}":
                return True
            elif not bot_username:
                return True
    
    return False

def starts_with_name_pattern(text: str) -> bool:
    """Проверяет, начинается ли текст с обращения к человеку (имя + запятая/двоеточие)."""
    # Паттерны типа: "Вася, ...", "Мама: ...", "Андрей привет"
    pattern = r'^[А-ЯЁA-Z][а-яёa-z]+[,:\s]'
    return bool(re.match(pattern, text.strip()))

def is_question_in_air(message: types.Message) -> bool:
    """
    Проверяет, является ли сообщение вопросом "в воздух".
    Вопрос в воздух — это вопрос, не адресованный конкретному человеку.
    """
    if not message.text:
        return False
    
    text = message.text.strip()
    
    # Должен заканчиваться на ?
    if not text.endswith('?'):
        return False
    
    # Не должен содержать упоминания других пользователей
    if has_other_mentions(message):
        return False
    
    # Не должен начинаться с имени (обращения к человеку)
    if starts_with_name_pattern(text):
        return False
    
    return True

async def get_chat_mode(chat_id: int) -> str:
    """Получает режим работы для чата из БД или кэша."""
    # Сначала проверяем кэш
    if chat_id in chat_modes_cache:
        return chat_modes_cache[chat_id]
    
    # Потом БД
    if db_conn:
        try:
            async with db_conn.execute(
                "SELECT mode FROM chat_settings WHERE chat_id = ?", 
                (chat_id,)
            ) as cursor:
                result = await cursor.fetchone()
                if result:
                    mode = result[0]
                    chat_modes_cache[chat_id] = mode
                    return mode
        except Exception as e:
            logger.error(f"Ошибка получения режима чата: {e}")
    
    # По умолчанию
    return DEFAULT_GROUP_MODE

async def set_chat_mode(chat_id: int, mode: str) -> bool:
    """Устанавливает режим работы для чата."""
    if mode not in CHAT_MODES:
        return False
    
    if db_conn:
        try:
            await db_conn.execute("""
                INSERT INTO chat_settings (chat_id, mode) VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET mode = ?, updated_at = CURRENT_TIMESTAMP
            """, (chat_id, mode, mode))
            await db_conn.commit()
            
            # Обновляем кэш
            chat_modes_cache[chat_id] = mode
            logger.info(f"📝 Chat {chat_id} mode set to: {mode}")
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения режима чата: {e}")
            return False
    
    return False

async def should_respond_in_group(message: types.Message) -> bool:
    """
    Определяет, должен ли бот отвечать на сообщение в группе.
    Логика зависит от режима чата.
    """
    chat_id = message.chat.id
    mode = await get_chat_mode(chat_id)
    
    logger.debug(f"Chat {chat_id} mode: {mode}")
    
    if mode == "off":
        # Только команды (обрабатываются отдельно)
        return False
    
    if mode == "all":
        # Отвечаем на всё
        return True
    
    if mode == "mention":
        # Только упоминания и ответы на бота
        if is_bot_mentioned(message):
            logger.info(f"📢 Bot mentioned in chat {chat_id}")
            return True
        if is_reply_to_bot(message):
            logger.info(f"↩️ Reply to bot in chat {chat_id}")
            return True
        return False
    
    if mode == "smart":
        # Умный режим: упоминания + ответы + вопросы в воздух
        if is_bot_mentioned(message):
            logger.info(f"📢 Bot mentioned in chat {chat_id}")
            return True
        if is_reply_to_bot(message):
            logger.info(f"↩️ Reply to bot in chat {chat_id}")
            return True
        if is_question_in_air(message):
            logger.info(f"❓ Question in air detected in chat {chat_id}: '{message.text[:50]}...'")
            return True
        return False
    
    # Неизвестный режим — по умолчанию mention
    return is_bot_mentioned(message) or is_reply_to_bot(message)

def remove_bot_mention(text: str, bot_username: str = None) -> str:
    """Удаляет упоминание бота из текста."""
    if not bot_username:
        return text
    # Удаляем @username в любом регистре
    pattern = re.compile(re.escape(f"@{bot_username}"), re.IGNORECASE)
    text = pattern.sub("", text).strip()
    return text

async def get_group_context_id(message: types.Message) -> int:
    """Возвращает ID для контекста (группы или пользователя)."""
    if await is_group_chat(message):
        return message.chat.id
    return message.from_user.id

# ============================================
# ФУНКЦИИ РАБОТЫ С OLLAMA
# ============================================

async def check_ollama() -> bool:
    """Проверяет доступность Ollama при старте."""
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.ok:
            models = response.json().get('models', [])
            logger.info(f"✅ Ollama доступна. Найдено моделей: {len(models)}")
            
            model_names = [m.get('name', '') for m in models]
            
            if not any(DEFAULT_MODEL in name for name in model_names):
                logger.warning(f"⚠️ Модель {DEFAULT_MODEL} не найдена")
            
            if not any(DEEP_MODEL in name for name in model_names):
                logger.warning(f"⚠️ Модель {DEEP_MODEL} не найдена")
            
            return True
    except requests.exceptions.ConnectionError:
        logger.error("❌ Ollama недоступна! Запусти: ollama serve")
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка проверки Ollama: {e}")
        return False

def call_ollama_stream(model: str, prompt: str, timeout: int = REQUEST_TIMEOUT, temperature: float = 0.8) -> str:
    """Отправляет запрос к Ollama и возвращает ответ."""
    logger.info(f"🔗 Connecting to Ollama: {OLLAMA_URL}")
    logger.info(f"🧠 Model: {model}, Temperature: {temperature}")
    
    payload = {
        "model": model,
        "prompt": prompt,
        "temperature": temperature,
        "top_p": 0.95,
        "top_k": 50,
        "num_ctx": 8192,  # ВАЖНО: увеличенный контекст
        "stream": True
    }
    
    try:
        logger.info(f"📡 Sending request to Ollama...")
        response = requests.post(OLLAMA_URL, json=payload, stream=True, timeout=timeout)
        response.raise_for_status()
        
        logger.info(f"📥 Receiving stream from Ollama...")
        full_response = ""
        chunk_count = 0
        
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "response" in obj:
                    full_response += obj["response"]
                    chunk_count += 1
                    if chunk_count % 10 == 0:
                        logger.debug(f"📊 Received {chunk_count} chunks, {len(full_response)} chars so far")
                if obj.get("error"):
                    logger.error(f"❌ Ollama error: {obj['error']}")
                    return f"Ошибка модели Ollama: {obj['error']}"
            except json.JSONDecodeError:
                continue
        
        logger.info(f"✅ Stream complete: {chunk_count} chunks, {len(full_response)} chars total")
        return full_response.strip()
        
    except requests.exceptions.Timeout:
        logger.error(f"⏱️ Timeout after {timeout}s")
        return "⏱️ Превышен таймаут ответа от Ollama."
    except requests.exceptions.ConnectionError:
        logger.error("❌ Connection error to Ollama")
        return "❌ Не удалось подключиться к Ollama. Проверьте что она запущена: ollama serve"
    except Exception as e:
        logger.exception(f"❌ Ollama call failed: {e}")
        return f"❌ Ошибка при обращении к модели: {e}"

def call_ollama_with_context(model: str, prompt: str, context_docs: list, timeout: int = REQUEST_TIMEOUT, temperature: float = 0.8) -> str:
    """Отправляет запрос к Ollama с контекстом из документов"""
    context_parts = []
    for i, doc in enumerate(context_docs, 1):
        source = doc['source']
        content = doc['content']
        context_parts.append(f"[Источник {i}: {source}]\n{content}\n")
    
    context_text = "\n---\n".join(context_parts)
    
    # v2.1: Добавлены языковые якоря для предотвращения переключения на китайский
    full_prompt = (
        "[ИНСТРУКЦИЯ: Отвечай ТОЛЬКО на русском языке. Never use Chinese.]\n\n"
        f"Ты - ТОР, русскоязычный AI-ассистент. У тебя есть доступ к следующим документам:\n\n"
        f"{context_text}\n"
        f"---\n\n"
        f"Используя информацию из этих документов, ответь на вопрос пользователя.\n"
        f"Если в документах нет ответа, честно скажи об этом.\n"
        f"Указывай источники, откуда взята информация.\n\n"
        f"Вопрос: {prompt}\n\n"
        f"Ответ на русском языке:"
    )
    
    return call_ollama_stream(model, full_prompt, timeout, temperature)

# ============================================
# ФУНКЦИИ РАБОТЫ С БД
# ============================================

async def init_db():
    """Инициализирует базу данных."""
    global db_conn
    db_conn = await aiosqlite.connect(DB_PATH)
    
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            prompt_hash TEXT PRIMARY KEY,
            prompt TEXT,
            response TEXT,
            model TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            prompt TEXT,
            response TEXT,
            model TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS user_activity (
            user_id INTEGER PRIMARY KEY,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            message_count INTEGER DEFAULT 0
        )
    """)
    
    # v2.2: Таблица настроек чатов
    await db_conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            mode TEXT DEFAULT 'smart',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Индексы для ускорения запросов
    await db_conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_user_id ON logs(user_id)")
    await db_conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_timestamp ON cache(timestamp)")
    
    await db_conn.commit()
    logger.info("✅ База данных инициализирована.")

def prompt_hash(prompt: str, model: str) -> str:
    return hashlib.sha256((prompt + "|" + model).encode("utf-8")).hexdigest()

async def get_cached(prompt: str, model: str):
    if db_conn is None:
        logger.warning("⚠️ БД не инициализирована, кэш недоступен")
        return None
    
    h = prompt_hash(prompt, model)
    try:
        async with db_conn.execute("SELECT response FROM cache WHERE prompt_hash = ?", (h,)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"❌ Ошибка чтения кэша: {e}")
        return None

async def save_cache(prompt: str, model: str, response: str):
    if db_conn is None:
        return
    
    h = prompt_hash(prompt, model)
    try:
        await db_conn.execute(
            "INSERT OR REPLACE INTO cache (prompt_hash, prompt, response, model) VALUES (?, ?, ?, ?)",
            (h, prompt, response, model)
        )
        await db_conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения в кэш: {e}")

async def log_dialog(context_id: int, prompt: str, response: str, model: str):
    if db_conn is None:
        return
    
    try:
        await db_conn.execute(
            "INSERT INTO logs (user_id, prompt, response, model) VALUES (?, ?, ?, ?)",
            (context_id, prompt, response, model)
        )
        await db_conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка логирования диалога: {e}")

async def update_user_activity(user_id: int):
    if db_conn is None:
        return
    
    try:
        await db_conn.execute("""
            INSERT INTO user_activity (user_id, last_seen, message_count) 
            VALUES (?, CURRENT_TIMESTAMP, 1)
            ON CONFLICT(user_id) DO UPDATE SET 
                last_seen = CURRENT_TIMESTAMP,
                message_count = message_count + 1
        """, (user_id,))
        await db_conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка обновления активности: {e}")

async def get_dialogue_context(context_id: int) -> str:
    if db_conn is None:
        return ""
    
    query = """
        SELECT prompt, response FROM logs
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """
    try:
        async with db_conn.execute(query, (context_id, CONTEXT_WINDOW * 2)) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return ""
        
        rows.reverse()
        
        context_parts = []
        for prompt, response in rows:
            cleaned_response = response.replace(" (cache)", "")
            context_parts.append(f"Пользователь: {prompt}\n")
            context_parts.append(f"Ассистент: {cleaned_response}\n")
            
        return "".join(context_parts)
    except Exception as e:
        logger.error(f"❌ Ошибка получения контекста: {e}")
        return ""

# ============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================

def split_text(text: str, max_length: int = MAX_TELEGRAM_LENGTH) -> list[str]:
    if not text:
        return [""]
    
    chunks = []
    while len(text) > max_length:
        split_index = text.rfind('\n\n', 0, max_length)
        if split_index == -1:
            split_index = text.rfind('. ', 0, max_length)
        if split_index == -1:
            split_index = text.rfind(' ', 0, max_length)
        if split_index == -1 or split_index == 0:
            split_index = max_length

        chunks.append(text[:split_index].strip())
        text = text[split_index:].strip()
    
    if text:
        chunks.append(text)
    
    return chunks

async def send_long_message(message: types.Message, text: str, parse_mode: str = "HTML"):
    """Отправляет длинное сообщение, разбивая на части."""
    chunks = split_text(text)
    
    for i, chunk in enumerate(chunks):
        try:
            if i == 0:
                await message.reply(chunk, parse_mode=parse_mode)
            else:
                await message.answer(chunk, parse_mode=parse_mode)
        except TelegramBadRequest as e:
            logger.warning(f"Ошибка форматирования в части {i+1}: {e}. Отправка без форматирования.")
            try:
                if i == 0:
                    await message.reply(chunk, parse_mode=None)
                else:
                    await message.answer(chunk, parse_mode=None)
            except Exception as e2:
                logger.error(f"❌ Не удалось отправить сообщение: {e2}")

async def show_typing_periodic(chat_id: int, stop_event: asyncio.Event):
    """Периодически отправляет индикатор набора текста"""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        try:
            # Ждём 5 секунд или пока не придёт сигнал остановки
            await asyncio.wait_for(stop_event.wait(), timeout=5)
            break
        except asyncio.TimeoutError:
            continue

# ============================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    is_group = await is_group_chat(message)
    
    if is_group:
        bot_username = bot._me.username if hasattr(bot, '_me') and bot._me else "бота"
        mode = await get_chat_mode(message.chat.id)
        mode_desc = CHAT_MODES.get(mode, "неизвестен")
        
        await message.reply(
            f"👋 Привет! Я <b>ТОР</b> (Творческий Олимпийский Разум).\n\n"
            f"📍 Режим чата: <b>{mode}</b>\n"
            f"{mode_desc}\n\n"
            f"Упомяните меня (@{escape_html(bot_username)}) или задайте вопрос!\n\n"
            "Команды: /help /chat_mode",
            parse_mode="HTML"
        )
    else:
        queue_info = await request_queue.get_queue_info()
        rag_status = "🟢 Активна" if RAG_ENABLED else "🔴 Неактивна"
        
        await message.reply(
            "⚡ <b>ТОР</b> — Творческий Олимпийский Разум\n\n"
            "Привет! Я ваш AI-ассистент на базе LLM.\n\n"
            "💬 Просто пиши — я отвечу\n"
            "🔥 /deep — мощная модель\n"
            "🗑️ /clear — очистить историю\n"
            "📊 /stats — статистика\n"
            "📋 /queue — состояние очереди\n"
            "🌡️ /temp — температура (0.1-1.5)\n"
            "📚 /ask — вопрос по документам\n"
            "🔧 /rag_init — активировать RAG\n"
            "📊 /rag_stats — статистика документов\n"
            "❓ /about — обо мне\n"
            "❓ /help — справка\n\n"
            f"🌡️ Температура: {CURRENT_TEMPERATURE}\n"
            f"📚 RAG: {rag_status}\n"
            f"📋 Очередь: {queue_info['queued']}, Активных: {queue_info['active']}",
            parse_mode="HTML"
        )

@dp.message(Command("chat_mode"))
async def cmd_chat_mode(message: types.Message):
    """Настройка режима работы бота в чате"""
    # Только для групп
    if not await is_group_chat(message):
        await message.reply("ℹ️ Эта команда работает только в групповых чатах")
        return
    
    # Только для админов
    if not await is_user_admin(message):
        await message.reply("⛔ Только администраторы могут менять режим чата")
        return
    
    # Парсим аргументы
    parts = message.text.split()
    
    if len(parts) < 2:
        # Показываем текущий режим и варианты
        current_mode = await get_chat_mode(message.chat.id)
        
        modes_list = "\n".join([
            f"• <code>{mode}</code> — {desc}" 
            for mode, desc in CHAT_MODES.items()
        ])
        
        await message.reply(
            f"⚙️ <b>Режим чата</b>\n\n"
            f"Текущий режим: <b>{current_mode}</b>\n"
            f"{CHAT_MODES.get(current_mode, '')}\n\n"
            f"<b>Доступные режимы:</b>\n{modes_list}\n\n"
            f"Использование: <code>/chat_mode режим</code>\n"
            f"Пример: <code>/chat_mode smart</code>",
            parse_mode="HTML"
        )
        return
    
    new_mode = parts[1].lower()
    
    if new_mode not in CHAT_MODES:
        await message.reply(
            f"❌ Неизвестный режим: <code>{escape_html(new_mode)}</code>\n\n"
            f"Доступные: <code>smart</code>, <code>mention</code>, <code>all</code>, <code>off</code>",
            parse_mode="HTML"
        )
        return
    
    # Устанавливаем новый режим
    if await set_chat_mode(message.chat.id, new_mode):
        await message.reply(
            f"✅ Режим чата изменён на: <b>{new_mode}</b>\n\n"
            f"{CHAT_MODES[new_mode]}",
            parse_mode="HTML"
        )
    else:
        await message.reply("❌ Ошибка при сохранении режима")

@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    """Показывает состояние очереди запросов"""
    info = await request_queue.get_queue_info()
    stats = info['stats']
    
    await message.reply(
        f"📋 <b>Состояние очереди:</b>\n\n"
        f"🔄 Активных запросов: {info['active']}/{info['max_concurrent']}\n"
        f"⏳ В очереди: {info['queued']}\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"✅ Обработано: {stats['total_processed']}\n"
        f"📥 Поставлено в очередь: {stats['total_queued']}\n"
        f"❌ Отклонено: {stats['total_rejected']}\n"
        f"⏱️ Среднее время ожидания: {stats['avg_wait_time']:.1f}с",
        parse_mode="HTML"
    )

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    if await is_group_chat(message):
        if not await is_user_admin(message):
            await message.reply("⛔ Только для админов")
            return
    
    if db_conn is None:
        await message.reply("❌ База данных не инициализирована")
        return
    
    context_id = await get_group_context_id(message)
    
    try:
        await db_conn.execute("DELETE FROM logs WHERE user_id = ?", (context_id,))
        await db_conn.commit()
        
        chat_type = "группы" if await is_group_chat(message) else "диалога"
        await message.reply(f"🗑️ История {chat_type} очищена!")
    except Exception as e:
        logger.error(f"❌ Ошибка очистки истории: {e}")
        await message.reply("❌ Ошибка при очистке истории")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if await is_group_chat(message):
        if not await is_user_admin(message):
            await message.reply("⛔ Только для админов")
            return
    
    if db_conn is None:
        await message.reply("❌ База данных не инициализирована")
        return
    
    context_id = await get_group_context_id(message)
    
    try:
        async with db_conn.execute("SELECT COUNT(*) FROM logs WHERE user_id = ?", (context_id,)) as cursor:
            messages_count = (await cursor.fetchone())[0]
        
        async with db_conn.execute("SELECT COUNT(*) FROM cache") as cursor:
            cache_count = (await cursor.fetchone())[0]
        
        queue_info = await request_queue.get_queue_info()
        
        # Для групп показываем режим чата
        extra_info = ""
        if await is_group_chat(message):
            mode = await get_chat_mode(message.chat.id)
            extra_info = f"🎯 Режим чата: {mode}\n"
        
        await message.reply(
            f"📊 <b>Статистика:</b>\n\n"
            f"💬 Сообщений: {messages_count}\n"
            f"🗄️ В кэше: {cache_count}\n"
            f"🧠 Контекст: {CONTEXT_WINDOW} пар\n"
            f"🌡️ Температура: {CURRENT_TEMPERATURE}\n"
            f"{extra_info}"
            f"📋 Очередь: {queue_info['active']}/{queue_info['max_concurrent']} активных",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"❌ Ошибка получения статистики: {e}")
        await message.reply("❌ Ошибка при получении статистики")

@dp.message(Command("temp"))
async def cmd_temp(message: types.Message):
    if await is_group_chat(message):
        if not await is_user_admin(message):
            await message.reply("⛔ Только для админов")
            return
    
    global CURRENT_TEMPERATURE
    
    try:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply(
                f"🌡️ Текущая температура: <b>{CURRENT_TEMPERATURE}</b>\n\n"
                "Формат: <code>/temp 0.8</code>\n"
                "Диапазон: 0.1 — 1.5\n\n"
                "💡 Выше = креативнее, ниже = точнее",
                parse_mode="HTML"
            )
            return
        
        temp = float(parts[1])
        if temp < 0.1 or temp > 1.5:
            await message.reply("⚠️ Допустимый диапазон: от 0.1 до 1.5")
            return
        
        CURRENT_TEMPERATURE = temp
        await message.reply(f"🌡️ Температура установлена: <b>{temp}</b>", parse_mode="HTML")
    except ValueError:
        await message.reply("❌ Неверный формат. Используйте число, например: <code>/temp 0.8</code>", parse_mode="HTML")

@dp.message(Command("about"))
async def cmd_about(message: types.Message):
    """Информация о боте"""
    rag_status = "🟢 Активна" if RAG_ENABLED else "🔴 Неактивна"
    
    await message.reply(
        "⚡ <b>ТОР</b> — Творческий Олимпийский Разум\n\n"
        "🤖 Я — AI-ассистент на базе больших языковых моделей (LLM)\n\n"
        "<b>💪 Мои возможности:</b>\n"
        "• Ответы на вопросы любой сложности\n"
        "• Помощь с задачами и планированием\n"
        "• Творческое мышление и генерация идей\n"
        "• Работа с контекстом диалога\n"
        "• Два режима: обычный и Deep (мощный)\n"
        "• Поиск по вашим документам через RAG\n"
        "• Умная работа в групповых чатах\n\n"
        "<b>🧠 Модели:</b>\n"
        f"• Обычная: <code>{escape_html(DEFAULT_MODEL)}</code>\n"
        f"• Deep: <code>{escape_html(DEEP_MODEL)}</code>\n\n"
        f"<b>📚 RAG система:</b> {rag_status}\n\n"
        "<b>⚙️ Технологии:</b>\n"
        "• Ollama (локальный запуск LLM)\n"
        "• GPU ускорение\n"
        "• Система очередей\n"
        "• Умное кэширование\n"
        "• Векторная база знаний\n\n"
        "💡 Создан для помощи людям!\n"
        "Автор: Bauyrzhan Khamzin",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Справка по командам"""
    rag_status = "🟢" if RAG_ENABLED else "🔴"
    is_group = await is_group_chat(message)
    
    group_help = ""
    if is_group:
        mode = await get_chat_mode(message.chat.id)
        group_help = (
            f"\n<b>Групповой чат (режим: {mode}):</b>\n"
            "/chat_mode — настроить режим ответов\n\n"
        )
    
    await message.reply(
        "📖 <b>СПРАВКА ПО КОМАНДАМ</b>\n\n"
        "<b>Основные:</b>\n"
        "💬 Просто напиши — я отвечу\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/about — информация о боте\n\n"
        "<b>Режимы работы:</b>\n"
        "/deep — мощная модель (медленнее, умнее)\n"
        "/clear — очистить историю диалога\n"
        f"{group_help}"
        "<b>Настройки:</b>\n"
        "/temp — изменить температуру (0.1-1.5)\n"
        "/stats — статистика использования\n"
        "/queue — состояние очереди\n\n"
        f"<b>RAG система {rag_status}:</b>\n"
        "/rag_init — активировать поиск по документам\n"
        "/rag_stats — статистика базы знаний\n"
        "/rag_clear — очистить базу документов\n"
        "/ask &lt;вопрос&gt; — вопрос по документам\n\n"
        "💡 <b>Температура:</b> выше = креативнее, ниже = точнее\n"
        "📚 <b>RAG:</b> поиск ответов в ваших документах",
        parse_mode="HTML"
    )

@dp.message(Command("deep"))
async def cmd_deep(message: types.Message, state: FSMContext):
    await state.set_state(BotStates.deep_mode)
    
    queue_info = await request_queue.get_queue_info()
    
    await message.reply(
        f"🔥 <b>Режим Deep активирован!</b>\n\n"
        f"Модель: <b>{escape_html(DEEP_MODEL)}</b>\n"
        f"📋 Очередь: {queue_info['queued']}, Активных: {queue_info['active']}\n\n"
        f"⚠️ <i>Первый запрос может занять 3-5 минут</i>\n\n"
        f"Напишите ваш вопрос:",
        parse_mode="HTML"
    )

@dp.message(Command("rag_init"))
async def cmd_rag_init(message: types.Message):
    """Инициализация RAG системы"""
    if await is_group_chat(message):
        if not await is_user_admin(message):
            await message.reply("⛔ Только для админов")
            return
    
    await message.reply("🔄 Инициализация RAG системы...")
    
    global RAG_ENABLED
    
    try:
        success = rag_manager.initialize()
        
        if success:
            RAG_ENABLED = True
            stats = rag_manager.get_stats()
            
            await message.reply(
                f"✅ <b>RAG система активирована!</b>\n\n"
                f"📊 Статистика:\n"
                f"• Всего чанков: {stats.get('total_chunks', 0)}\n"
                f"• Документов: {stats.get('total_sources', 0)}\n\n"
                f"💡 Используй <code>/ask вопрос</code> для вопросов по документам\n"
                f"📋 /rag_stats — статистика базы",
                parse_mode="HTML"
            )
        else:
            await message.reply("❌ Не удалось инициализировать RAG")
    
    except Exception as e:
        logger.exception(f"Ошибка инициализации RAG: {e}")
        await message.reply(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

@dp.message(Command("rag_stats"))
async def cmd_rag_stats(message: types.Message):
    """Статистика RAG базы"""
    if not RAG_ENABLED:
        await message.reply("⚠️ RAG не активирована. Используй /rag_init")
        return
    
    stats = rag_manager.get_stats()
    
    if stats['status'] == 'ready':
        # Экранируем имена файлов для HTML (важно для файлов с _)
        sources_lines = []
        for source, count in stats.get('sources', {}).items():
            safe_source = escape_html(source)
            sources_lines.append(f"   • <code>{safe_source}</code>: {count} чанков")
        sources_text = "\n".join(sources_lines) if sources_lines else "   (пусто)"
        
        await message.reply(
            f"📊 <b>Статистика RAG базы:</b>\n\n"
            f"📦 Всего чанков: {stats['total_chunks']}\n"
            f"📄 Документов: {stats['total_sources']}\n\n"
            f"<b>📚 Источники:</b>\n{sources_text}",
            parse_mode="HTML"
        )
    else:
        await message.reply(f"❌ Статус: {escape_html(stats['status'])}", parse_mode="HTML")

@dp.message(Command("ask"))
async def cmd_ask(message: types.Message):
    """Вопрос по документам через RAG"""
    if not RAG_ENABLED:
        await message.reply("⚠️ RAG не активирована. Используй /rag_init")
        return
    
    text = message.text.replace("/ask", "").strip()
    if not text:
        await message.reply(
            "💡 <b>Как использовать:</b>\n"
            "<code>/ask ваш вопрос</code>\n\n"
            "Например:\n"
            "<code>/ask Что говорится о духовности?</code>\n"
            "<code>/ask Расскажи основные идеи из книги</code>",
            parse_mode="HTML"
        )
        return
    
    await message.reply("🔍 Ищу в документах...")
    
    try:
        relevant_docs = rag_manager.search(text, k=5)
        
        if not relevant_docs:
            await message.reply("❌ В базе документов ничего не найдено по вашему запросу")
            return
        
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(show_typing_periodic(message.chat.id, stop_typing))
        
        try:
            model = DEFAULT_MODEL
            
            await message.reply(
                f"💭 Думаю... <i>(найдено источников: {len(relevant_docs)})</i>",
                parse_mode="HTML"
            )
            
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                call_ollama_with_context,
                model,
                text,
                relevant_docs,
                REQUEST_TIMEOUT,
                CURRENT_TEMPERATURE
            )
            
            stop_typing.set()
            await typing_task
            
            # Экранируем имена файлов
            sources_list = list(set([doc['source'] for doc in relevant_docs]))
            sources_text = "\n".join([f"• <code>{escape_html(s)}</code>" for s in sources_list])
            
            # Экранируем ответ модели
            safe_response = escape_html(response)
            
            final_response = (
                f"{safe_response}\n\n"
                f"<b>📚 Источники:</b>\n{sources_text}"
            )
            
            await send_long_message(message, final_response, parse_mode="HTML")
            
            context_id = await get_group_context_id(message)
            await log_dialog(context_id, text, response, f"{model} (RAG)")
            
        except Exception as e:
            stop_typing.set()
            await typing_task
            logger.exception(f"Ошибка генерации ответа: {e}")
            await message.reply(f"❌ Ошибка генерации: {escape_html(str(e))}", parse_mode="HTML")
    
    except Exception as e:
        logger.exception(f"Ошибка RAG поиска: {e}")
        await message.reply(f"❌ Ошибка поиска: {escape_html(str(e))}", parse_mode="HTML")

@dp.message(Command("rag_clear"))
async def cmd_rag_clear(message: types.Message):
    """Очистка RAG базы"""
    if await is_group_chat(message):
        if not await is_user_admin(message):
            await message.reply("⛔ Только для админов")
            return
    
    if not RAG_ENABLED:
        await message.reply("⚠️ RAG не активирована")
        return
    
    await message.reply(
        "⚠️ <b>Внимание!</b>\n\n"
        "Все документы будут удалены из базы!\n\n"
        "Для подтверждения отправьте: <code>да, удалить</code>",
        parse_mode="HTML"
    )

# ============================================
# ОБРАБОТЧИКИ СООБЩЕНИЙ
# ============================================

async def process_message(message: types.Message, model: str, is_deep: bool = False):
    """Общая логика обработки сообщений."""
    user_text = message.text.strip()
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "Unknown"
    
    if hasattr(bot, '_me') and bot._me:
        user_text = remove_bot_mention(user_text, bot._me.username)
    
    # Проверка на пустой текст после удаления упоминания
    if not user_text:
        await message.reply("❓ Напишите ваш вопрос")
        return
    
    logger.info(f"📨 User {username} (ID: {user_id}): '{user_text[:50]}{'...' if len(user_text) > 50 else ''}'")
    logger.info(f"🤖 Model: {model}, Temp: {CURRENT_TEMPERATURE}, Deep: {is_deep}")
    
    await update_user_activity(user_id)
    
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(show_typing_periodic(message.chat.id, stop_typing))
    
    try:
        context_id = await get_group_context_id(message)
        dialogue_context = await get_dialogue_context(context_id)
        
        system_instruction = (
            "Ты - ТОР, дружелюбный AI-ассистент. "
            "Твоё имя - ТОР (сокращение от 'Творческий Олимпийский Разум'). "
            "Когда тебя спрашивают о твоём имени, представляйся: 'Я - ТОР, ваш AI-помощник!' "
            "Отвечай ТОЛЬКО на последний вопрос пользователя. "
            "Не повторяй историю диалога. "
            "Будь креативным, полезным и точным. "
            "Пиши кратко на русском языке."
        )
        
        full_prompt = (
            f"{system_instruction}\n\n"
            f"{dialogue_context}"
            f"Пользователь: {user_text}\n"
            f"Ассистент:"
        )

        cached = await get_cached(full_prompt, model)
        if cached:
            logger.info(f"💾 Cache hit")
            stop_typing.set()
            await typing_task
            response_text = f"{escape_html(cached)}\n\n💾 <i>(из кэша)</i>"
            await send_long_message(message, response_text, parse_mode="HTML")
            await log_dialog(context_id, user_text, cached + " (cache)", model)
            return
        
        logger.info(f"🔄 Generating...")
        mode_emoji = "🔥" if is_deep else "💭"
        
        timeout = REQUEST_TIMEOUT * 2 if is_deep else REQUEST_TIMEOUT
        
        await message.reply(
            f"{mode_emoji} Думаю... <i>(модель <b>{escape_html(model)}</b>)</i>",
            parse_mode="HTML"
        )
        
        logger.info(f"⚙️ Calling Ollama (timeout: {timeout}s)...")
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, 
            call_ollama_stream, 
            model, 
            full_prompt, 
            timeout,
            CURRENT_TEMPERATURE
        )
        
        logger.info(f"✅ Response: {len(response)} chars")
        stop_typing.set()
        await typing_task

        if not response:
            response = "❌ Пустой ответ от модели"
        
        if "❌" not in response and "⏱️" not in response:
            await save_cache(full_prompt, model, response)
        
        logger.info(f"📤 Sending to {context_id}")
        await log_dialog(context_id, user_text, response, model)
        
        # Экранируем ответ для HTML
        safe_response = escape_html(response)
        await send_long_message(message, safe_response, parse_mode="HTML")
        logger.info(f"✅ Completed for {context_id}")
        
    except Exception as e:
        stop_typing.set()
        await typing_task
        logger.exception(f"❌ Error: {e}")
        await message.reply(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")

@dp.message(BotStates.deep_mode)
async def handle_deep_mode(message: types.Message, state: FSMContext):
    if not message.text:
        await state.clear()
        return
    
    if await request_queue.can_process():
        await state.clear()
        await request_queue.start_processing()
        try:
            await process_message(message, DEEP_MODEL, is_deep=True)
        finally:
            await request_queue.finish_processing()
    else:
        position = await request_queue.add_to_queue({
            'message': message,
            'model': DEEP_MODEL,
            'is_deep': True
        })
        await state.clear()
        
        if position == -1:
            await message.reply("❌ Очередь переполнена! Попробуйте позже.")
        else:
            queue_info = await request_queue.get_queue_info()
            await message.reply(
                f"⏳ Добавлено в очередь\n"
                f"📍 Позиция: {position}\n"
                f"🔄 Активных: {queue_info['active']}/{queue_info['max_concurrent']}",
                parse_mode="HTML"
            )

@dp.message()
async def handle_default(message: types.Message):
    if not message.text:
        return
    
    # Неизвестные команды
    if message.text.startswith('/'):
        logger.warning(f"⚠️ Неизвестная команда: {message.text}")
        await message.reply("❓ Неизвестная команда. Используй /help для списка команд")
        return
    
    # Подтверждение удаления RAG
    if message.text.lower() == "да, удалить" and RAG_ENABLED:
        try:
            if rag_manager.clear_database():
                await message.reply("✅ База документов очищена!")
            else:
                await message.reply("❌ Ошибка при очистке базы")
        except Exception as e:
            await message.reply(f"❌ Ошибка: {escape_html(str(e))}", parse_mode="HTML")
        return
    
    # В группах — проверяем режим чата
    if await is_group_chat(message):
        if not await should_respond_in_group(message):
            return
    
    logger.info(f"🎯 Handling from {message.from_user.id}")
    
    model = DEFAULT_MODEL
    
    if await request_queue.can_process():
        await request_queue.start_processing()
        try:
            await process_message(message, model)
        finally:
            await request_queue.finish_processing()
    else:
        position = await request_queue.add_to_queue({
            'message': message,
            'model': model,
            'is_deep': False
        })
        
        if position == -1:
            await message.reply("❌ Очередь переполнена! Попробуйте позже.")
        else:
            queue_info = await request_queue.get_queue_info()
            await message.reply(
                f"⏳ Добавлено в очередь\n"
                f"📍 Позиция: {position}\n"
                f"🔄 Активных: {queue_info['active']}/{queue_info['max_concurrent']}",
                parse_mode="HTML"
            )

# ============================================
# ЗАПУСК БОТА
# ============================================

async def main():
    if not await check_ollama():
        logger.error("🛑 Ollama недоступна!")
        return
    
    await init_db()
    
    me = await bot.get_me()
    bot._me = me
    
    logger.info("🚀 Запуск ТОР (Творческий Олимпийский Разум) v2.2...")
    logger.info(f"🤖 Bot: @{me.username}")
    logger.info(f"🌡️ Temperature: {CURRENT_TEMPERATURE}")
    logger.info(f"📋 Max concurrent: {MAX_CONCURRENT_REQUESTS}")
    logger.info(f"📊 Max queue size: {MAX_QUEUE_SIZE}")
    logger.info(f"🎯 Default group mode: {DEFAULT_GROUP_MODE}")
    
    queue_task = asyncio.create_task(queue_processor())
    
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        logger.info("⏹️ Остановка...")
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass
        if db_conn:
            await db_conn.close()
        await bot.session.close()
        logger.info("✅ Ресурсы освобождены")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Остановлен (Ctrl+C)")
    except Exception as e:
        logger.exception(f"❌ Ошибка: {e}")
