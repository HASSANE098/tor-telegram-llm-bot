"""
Скрипт для загрузки документов в RAG базу ТОРа
Использование: python load_documents.py
"""

import os
import sys
from pathlib import Path
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from rag_manager import rag_manager

def find_documents(documents_dir: str = "./documents") -> list:
    """
    Находит все поддерживаемые документы в папке
    
    Args:
        documents_dir: Папка с документами
        
    Returns:
        Список путей к найденным файлам
    """
    supported_extensions = ['.pdf', '.docx', '.txt']
    documents_path = Path(documents_dir)
    
    if not documents_path.exists():
        print(f"❌ Папка {documents_dir} не найдена!")
        print(f"📁 Создаю папку...")
        documents_path.mkdir(exist_ok=True)
        print(f"✅ Создана папка: {documents_path.absolute()}")
        print(f"📋 Поместите туда ваши документы (.pdf, .docx, .txt)")
        return []
    
    found_files = []
    
    for ext in supported_extensions:
        files = list(documents_path.glob(f"*{ext}"))
        found_files.extend(files)
    
    return [str(f) for f in found_files]


def main():
    """Основная функция загрузки документов"""
    
    print("=" * 60)
    print("  📚 ЗАГРУЗКА ДОКУМЕНТОВ В БАЗУ ТОРа")
    print("=" * 60)
    print()
    
    # Инициализация RAG
    print("🔄 Инициализация RAG системы...")
    if not rag_manager.initialize():
        print("❌ Не удалось инициализировать RAG!")
        return
    
    print("✅ RAG система готова")
    print()
    
    # Поиск документов
    print("🔍 Поиск документов в папке ./documents...")
    documents = find_documents("./documents")
    
    if not documents:
        print()
        print("⚠️  Документы не найдены!")
        print()
        print("📝 Инструкция:")
        print("   1. Создана папка: ./documents")
        print("   2. Поместите туда файлы:")
        print("      • PDF файлы (.pdf)")
        print("      • Word документы (.docx)")
        print("      • Текстовые файлы (.txt)")
        print("   3. Запустите скрипт снова")
        print()
        input("Нажмите Enter для выхода...")
        return
    
    print(f"✅ Найдено файлов: {len(documents)}")
    print()
    
    # Показываем список файлов
    print("📋 Файлы для загрузки:")
    for i, doc in enumerate(documents, 1):
        file_name = Path(doc).name
        file_size = Path(doc).stat().st_size / 1024  # KB
        print(f"   {i}. {file_name} ({file_size:.1f} KB)")
    print()
    
    # Подтверждение
    response = input("Загрузить эти документы? (y/n): ").strip().lower()
    
    if response != 'y':
        print("❌ Загрузка отменена")
        return
    
    print()
    print("=" * 60)
    print("⏳ ЗАГРУЗКА ДОКУМЕНТОВ (может занять несколько минут)...")
    print("=" * 60)
    print()
    
    # Загружаем документы
    total_chunks = rag_manager.add_documents(documents)
    
    print()
    print("=" * 60)
    
    if total_chunks > 0:
        print(f"✅ УСПЕШНО ЗАГРУЖЕНО!")
        print(f"📊 Статистика:")
        
        stats = rag_manager.get_stats()
        print(f"   • Всего чанков: {stats['total_chunks']}")
        print(f"   • Документов: {stats['total_sources']}")
        print()
        print("📁 Загруженные файлы:")
        for source, count in stats['sources'].items():
            print(f"   • {source}: {count} чанков")
        print()
        print("🎉 ТОР теперь знает содержимое ваших документов!")
        print("💬 Можете задавать вопросы боту по этим материалам")
    else:
        print("❌ Не удалось загрузить документы")
    
    print("=" * 60)
    print()
    input("Нажмите Enter для выхода...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Загрузка прервана пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        input("\nНажмите Enter для выхода...")