"""Функции для загрузки данных из CSV в графовую БД.

Использовать примерно так, сначала ноды, потом узлы:
load_nodes_from_csv(neo4j_driver, "../data/persons.csv")
load_nodes_from_csv(neo4j_driver, "../data/departments.csv")
load_nodes_from_csv(neo4j_driver, "../data/publications.csv")
load_relationships_from_csv(neo4j_driver, "../data/affiliation.csv")
load_relationships_from_csv(neo4j_driver, "../data/authorship.csv")

Или через main.
"""

import csv
import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from neo4j_client import Neo4jClient, neo4j_driver


def load_nodes_from_csv(client: Neo4jClient, csv_path: str, batch_size: int = 2000):
    """
    Универсальная загрузка узлов из CSV.
    Столбцы: id, labels, properties
    """
    batches_by_labels = defaultdict(list)

    with open(csv_path, mode='r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            node_id = row['id']
            labels = row['labels'].strip()
            properties_str = row.get('properties', '{}')
            properties = json.loads(properties_str) if properties_str else {}

            batches_by_labels[labels].append((node_id, properties))
            
            if len(batches_by_labels[labels]) >= batch_size:
                client.upsert_nodes_batch(labels, batches_by_labels[labels])
                batches_by_labels[labels].clear()

    for labels, batch in batches_by_labels.items():
        if batch:
            client.upsert_nodes_batch(labels, batch)


def load_relationships_from_csv(client: Neo4jClient, csv_path: str, batch_size: int = 2000):
    """
    Универсальная загрузка связей из CSV.
    Столбцы: start_id, end_id, src_label, tgt_label, type, properties
    """
    batches_by_rel = defaultdict(list)

    with open(csv_path, mode='r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        for row in reader:
            src_id = row['start_id']
            tgt_id = row['end_id']
            src_label = row['src_label'].strip()
            tgt_label = row['tgt_label'].strip()
            rel_type = row['type'].strip()
            properties_str = row.get('properties', '{}')
            properties = json.loads(properties_str) if properties_str else {}

            key = (src_label, tgt_label, rel_type)
            batches_by_rel[key].append((src_id, tgt_id, properties))

            if len(batches_by_rel[key]) >= batch_size:
                client.upsert_relationships_batch(src_label, tgt_label, rel_type, batches_by_rel[key])
                batches_by_rel[key].clear()
    
    for (src_label, tgt_label, rel_type), batch in batches_by_rel.items():
        if batch:
            client.upsert_relationships_batch(src_label, tgt_label, rel_type, batch)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


def discover_files(data_dir: Path, suffix: str) -> list[Path]:
    files = sorted(data_dir.glob(f"*{suffix}"))
    return files


def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    
    data_dir = Path("../data")
    
    if not data_dir.exists():
        logger.error(f"Директория с данными не найдена: {data_dir.absolute()}")
        return
    
    node_files = discover_files(data_dir, "_nodes.csv")
    rel_files = discover_files(data_dir, "_rels.csv")
    
    logger.info(f"Найдено файлов узлов: {len(node_files)}, файлов связей: {len(rel_files)}")
    
    start_time = time.time()

    try:
        if node_files:
            logger.info("--- Начало загрузки узлов ---")
            for file_path in node_files:
                logger.info(f"Обработка файла узлов: {file_path.name}")
                try:
                    load_nodes_from_csv(neo4j_driver, str(file_path))
                    logger.info(f"Успешно загружен файл: {file_path.name}")
                except Exception as e:
                    logger.error(f"Ошибка при загрузке файла {file_path.name}: {e}", exc_info=True)
        else:
            logger.info("Файлы узлов не найдены, пропускаем этап.")

        if rel_files:
            logger.info("--- Начало загрузки связей ---")
            for file_path in rel_files:
                logger.info(f"Обработка файла связей: {file_path.name}")
                try:
                    load_relationships_from_csv(neo4j_driver, str(file_path))
                    logger.info(f"Успешно загружен файл: {file_path.name}")
                except Exception as e:
                    logger.error(f"Ошибка при загрузке файла {file_path.name}: {e}", exc_info=True)
        else:
            logger.info("Файлы связей не найдены, пропускаем этап.")

    except Exception as e:
        logger.critical(f"Критическая ошибка в работе пайплайна: {e}", exc_info=True)
    finally:
        neo4j_driver.close()
        elapsed_time = time.time() - start_time
        logger.info(f"Пайплайн завершен. Общее время выполнения: {elapsed_time:.2f} сек.")


if __name__ == "__main__":
    main()