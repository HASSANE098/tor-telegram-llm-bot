import os
import logging
from typing import List, Dict
from pathlib import Path

try:
    from langchain_community.document_loaders import (
        PyPDFLoader,
        Docx2txtLoader,
        TextLoader
    )
    # Новая структура импортов LangChain
    try:
        from langchain.text_splitter import RecursiveCharacterTextSplitter
    except ImportError:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    
    from langchain_community.embeddings import HuggingFaceEmbeddings
    
    # Используем новый пакет langchain-chroma
    try:
        from langchain_chroma import Chroma
    except ImportError:
        from langchain_community.vectorstores import Chroma
        
    RAG_AVAILABLE = True
except ImportError as e:
    RAG_AVAILABLE = False
    RAG_IMPORT_ERROR = str(e)
    logging.warning(f"⚠️ RAG зависимости не установлены: {e}")

logger = logging.getLogger("rag_manager")

class RAGManager:
    """Управление документами и поиском через RAG"""
    
    def __init__(self, persist_directory: str = "./chroma_db"):
        """
        Инициализация RAG системы
        
        Args:
            persist_directory: Папка для хранения векторной базы
        """
        self.persist_directory = persist_directory
        self.embeddings = None
        self.vectorstore = None
        self.is_initialized = False
        
        # Создаём папку если её нет
        os.makedirs(persist_directory, exist_ok=True)
        
        logger.info(f"📁 RAG directory: {persist_directory}")
    
    def initialize(self):
        """Инициализирует embedding модель и векторную базу"""
        if not RAG_AVAILABLE:
            logger.error(f"❌ RAG недоступна! Ошибка импорта: {RAG_IMPORT_ERROR}")
            logger.error("💡 Установите зависимости: pip install langchain langchain-community chromadb sentence-transformers pypdf docx2txt")
            return False
        
        try:
            logger.info("🔄 Загрузка embedding модели...")
            logger.info("⏳ Первая загрузка займёт ~1-2 минуты (скачивание модели ~500MB)...")
            
            # Используем компактную русскоязычную модель
            self.embeddings = HuggingFaceEmbeddings(
                model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                model_kwargs={'device': 'cpu'},
                encode_kwargs={'normalize_embeddings': True}
            )
            
            logger.info("✅ Embedding модель загружена")
            
            # Загружаем или создаём векторную базу
            if os.path.exists(os.path.join(self.persist_directory, "chroma.sqlite3")):
                logger.info("📂 Загрузка существующей базы документов...")
                self.vectorstore = Chroma(
                    persist_directory=self.persist_directory,
                    embedding_function=self.embeddings
                )
                doc_count = len(self.vectorstore.get()['ids'])
                logger.info(f"✅ Загружено чанков: {doc_count}")
            else:
                logger.info("🆕 Создание новой базы документов...")
                self.vectorstore = Chroma(
                    persist_directory=self.persist_directory,
                    embedding_function=self.embeddings
                )
                logger.info("✅ База создана (пока пустая)")
            
            self.is_initialized = True
            return True
            
        except Exception as e:
            logger.exception(f"❌ Ошибка инициализации RAG: {e}")
            return False
    
    def load_document(self, file_path: str) -> List:
        """
        Загружает документ и разбивает на чанки
        
        Args:
            file_path: Путь к файлу
            
        Returns:
            Список чанков документа
        """
        if not RAG_AVAILABLE:
            logger.error("❌ RAG недоступна!")
            return []
        
        file_path = Path(file_path)
        
        if not file_path.exists():
            logger.error(f"❌ Файл не найден: {file_path}")
            return []
        
        try:
            # Выбираем загрузчик по расширению
            ext = file_path.suffix.lower()
            
            logger.info(f"📄 Загрузка: {file_path.name} ({ext})")
            
            if ext == '.pdf':
                loader = PyPDFLoader(str(file_path))
            elif ext == '.docx':
                loader = Docx2txtLoader(str(file_path))
            elif ext == '.txt':
                loader = TextLoader(str(file_path), encoding='utf-8')
            else:
                logger.error(f"❌ Неподдерживаемый формат: {ext}")
                logger.info("💡 Поддерживаются: .pdf, .docx, .txt")
                return []
            
            documents = loader.load()
            logger.info(f"✅ Загружено страниц/разделов: {len(documents)}")
            
            # Разбиваем на чанки
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000,
                chunk_overlap=200,
                length_function=len,
                separators=["\n\n", "\n", ". ", " ", ""]
            )
            
            chunks = text_splitter.split_documents(documents)
            
            # Добавляем метаданные
            for chunk in chunks:
                chunk.metadata['source_file'] = file_path.name
                chunk.metadata['file_type'] = ext
            
            logger.info(f"✅ Создано чанков: {len(chunks)}")
            return chunks
            
        except Exception as e:
            logger.exception(f"❌ Ошибка загрузки {file_path}: {e}")
            return []
    
    def add_documents(self, file_paths: List[str]) -> int:
        """
        Добавляет документы в векторную базу
        
        Args:
            file_paths: Список путей к файлам
            
        Returns:
            Количество добавленных чанков
        """
        # Проверяем инициализацию
        if not self.is_initialized or not self.vectorstore:
            logger.error("❌ RAG не инициализирована! Вызовите initialize() сначала")
            logger.info("🔄 Попытка автоматической инициализации...")
            if not self.initialize():
                logger.error("❌ Не удалось инициализировать RAG!")
                return 0
        
        total_chunks = 0
        successful_files = 0
        failed_files = 0
        
        for i, file_path in enumerate(file_paths, 1):
            logger.info(f"📁 [{i}/{len(file_paths)}] Обработка файла...")
            chunks = self.load_document(file_path)
            
            if chunks:
                try:
                    logger.info(f"💾 Добавление в векторную базу...")
                    self.vectorstore.add_documents(chunks)
                    total_chunks += len(chunks)
                    successful_files += 1
                    logger.info(f"✅ [{i}/{len(file_paths)}] Успешно: {Path(file_path).name} ({len(chunks)} чанков)")
                except Exception as e:
                    logger.exception(f"❌ [{i}/{len(file_paths)}] Ошибка добавления в базу: {e}")
                    failed_files += 1
            else:
                logger.warning(f"⚠️ [{i}/{len(file_paths)}] Пропущен: {Path(file_path).name}")
                failed_files += 1
        
        if total_chunks > 0:
            logger.info(f"💾 Сохранение векторной базы...")
            try:
                # В новой версии langchain-chroma persist не нужен (авто-сохранение)
                if hasattr(self.vectorstore, 'persist'):
                    self.vectorstore.persist()
                logger.info(f"✅ База сохранена успешно")
            except Exception as e:
                logger.warning(f"⚠️ Сохранение не требуется (авто-сохранение активно)")
            
            logger.info(f"📊 Итого: успешно={successful_files}, ошибок={failed_files}, чанков={total_chunks}")
        
        return total_chunks
    
    def search(self, query: str, k: int = 5) -> List[Dict]:
        """
        Поиск релевантных документов
        
        Args:
            query: Поисковый запрос
            k: Количество результатов
            
        Returns:
            Список найденных документов с метаданными
        """
        if not self.is_initialized:
            logger.error("❌ RAG не инициализирована!")
            return []
        
        if not self.vectorstore:
            logger.error("❌ Векторная база не инициализирована!")
            return []
        
        try:
            logger.info(f"🔍 Поиск по запросу: '{query[:50]}...'")
            results = self.vectorstore.similarity_search_with_score(query, k=k)
            
            formatted_results = []
            for doc, score in results:
                formatted_results.append({
                    'content': doc.page_content,
                    'source': doc.metadata.get('source_file', 'Unknown'),
                    'score': float(score),
                    'metadata': doc.metadata
                })
            
            logger.info(f"✅ Найдено результатов: {len(formatted_results)}")
            return formatted_results
            
        except Exception as e:
            logger.exception(f"❌ Ошибка поиска: {e}")
            return []
    
    def get_stats(self) -> Dict:
        """Возвращает статистику по базе документов"""
        if not self.is_initialized:
            return {
                'total_chunks': 0,
                'total_sources': 0,
                'sources': {},
                'status': 'not_initialized'
            }
        
        if not self.vectorstore:
            return {
                'total_chunks': 0,
                'total_sources': 0,
                'sources': {},
                'status': 'vectorstore_error'
            }
        
        try:
            data = self.vectorstore.get()
            doc_count = len(data['ids'])
            
            # Подсчёт документов по источникам
            sources = {}
            for metadata in data['metadatas']:
                source = metadata.get('source_file', 'Unknown')
                sources[source] = sources.get(source, 0) + 1
            
            return {
                'total_chunks': doc_count,
                'total_sources': len(sources),
                'sources': sources,
                'status': 'ready'
            }
        except Exception as e:
            logger.exception(f"❌ Ошибка получения статистики: {e}")
            return {
                'total_chunks': 0,
                'total_sources': 0,
                'sources': {},
                'status': 'error',
                'error': str(e)
            }
    
    def clear_database(self):
        """Очищает всю базу документов"""
        if not self.is_initialized:
            logger.error("❌ RAG не инициализирована!")
            return False
        
        try:
            if self.vectorstore:
                # Удаляем все документы
                data = self.vectorstore.get()
                if data['ids']:
                    self.vectorstore.delete(data['ids'])
                    if hasattr(self.vectorstore, 'persist'):
                        self.vectorstore.persist()
                    logger.info("🗑️ База документов очищена")
                    return True
                else:
                    logger.info("ℹ️ База уже пуста")
                    return True
            return False
        except Exception as e:
            logger.exception(f"❌ Ошибка очистки базы: {e}")
            return False


# Глобальный экземпляр (создаётся при импорте)
rag_manager = RAGManager()