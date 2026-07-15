"""
Миграция данных из SQLite в Neo4j (Graph-Native архитектура).
"""

import json
import sqlite3

from neo4j import GraphDatabase
from tqdm import tqdm

from config import (
    DB_PATH,
    NEO4J_URI,
    NEO4J_USER,
    NEO4J_PASSWORD
)

CHUNK_SIZE = 2000


def safe_json_loads(val):
    if not val:
        return []
    try:
        return json.loads(val)
    except json.JSONDecodeError:
        return []


def create_constraints(driver):
    queries = [
        "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Publication) REQUIRE p.id IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Department) REQUIRE d.id IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (r:Repository) REQUIRE r.id IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (r:Repository) REQUIRE r.url IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (gh:GitHubProfile) REQUIRE gh.id IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (gh:GitHubProfile) REQUIRE gh.login IS UNIQUE",
        "CREATE CONSTRAINT IF NOT EXISTS FOR (lc:LinkCandidate) REQUIRE lc.id IS UNIQUE"
    ]
    with driver.session() as session:
        for q in queries:
            session.run(q)


def fetch_and_load(sqlite_conn, neo4j_driver, extract_query, cypher_query, desc, transform_func=None):
    cursor = sqlite_conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM ({extract_query})")
    total_rows = cursor.fetchone()[0]
    
    if total_rows == 0:
        print(f"Нет данных для: {desc}")
        return
    
    cursor.execute(extract_query)
    columns = [col[0] for col in cursor.description]
    
    with tqdm(total=total_rows, desc=desc) as pbar:
        while True:
            rows = cursor.fetchmany(CHUNK_SIZE)
            if not rows:
                break
            
            batch = [dict(zip(columns, row)) for row in rows]
            
            if transform_func:
                batch = [transform_func(row) for row in batch]
            
            with neo4j_driver.session() as session:
                session.execute_write(lambda tx: tx.run(cypher_query, batch=batch))
            
            pbar.update(len(batch))


def run_migration():
    print("=" * 70)
    print("ЗАПУСК МИГРАЦИИ SQLite -> NEO4J (GRAPH-NATIVE MAPPING)")
    print("=" * 70)
    
    sqlite_conn = sqlite3.connect(DB_PATH)
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    neo4j_driver.verify_connectivity()
    
    try:
        create_constraints(neo4j_driver)
        print("Констрейнты уникальности проверены/созданы.\n")

        # ==========================================
        # 1. ЗАГРУЗКА УЗЛОВ (NODES)
        # ==========================================

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM departments",
            """
            UNWIND $batch AS row
            MERGE (d:Department {id: row.id})
            SET d.name_ru = row.name_ru,
                d.name_en = row.name_en,
                d.name_variants = row.name_variants
            """,
            "Узлы: Department",
            transform_func=lambda r: {**r, 'name_variants': safe_json_loads(r['name_variants'])}
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM github_departments",
            """
            UNWIND $batch AS row
            MERGE (gh:GitHubProfile {id: row.id})
            SET gh.login = row.github_login,
                gh.name = row.name,
                gh.html_url = row.html_url,
                gh.description = row.description,
                gh.location = row.location,
                gh.type = 'org'
            """,
            "Узлы: GitHubProfile (Организации)"
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM publications",
            """
            UNWIND $batch AS row
            MERGE (pub:Publication {id: row.id})
            SET pub.title = row.title,
                pub.journal = row.journal,
                pub.doi = row.doi,
                pub.publication_date = date(row.publication_date),
                pub.year = toInteger(row.year),
                pub.has_code = (row.has_code = 1),
                pub.code_url = row.code_url,
                pub.funding = row.funding,
                pub.openalex_url = row.openalex_url,
                pub.pdf_url = row.pdf_url,
                pub.abstract = row.abstract
            """,
            "Узлы: Publication"
        )

        def transform_person_itmo(row):
            row['name_variants'] = safe_json_loads(row['name_variants'])
            row['dept_ids'] = row['department'].split('; ') if row['department'] else []
            return row

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM persons_itmo",
            """
            UNWIND $batch AS row
            MERGE (p:Person:Itmo {id: row.id})
            SET p.name_en = row.name_en,
                p.first_name_ru = row.first_name_ru,
                p.second_name_ru = row.second_name_ru,
                p.surname_ru = row.surname_ru,
                p.name_variants = row.name_variants,
                p.degree = row.degree,
                p.email = row.email,
                p.github = row.github,
                p.google_scholar = row.google_scholar,
                p.openreview = row.openreview,
                p.thesis = row.thesis,
                p.created_at = row.created_at

            // Сразу создаем связи с департаментами из распарсенного поля
            WITH p, row
            UNWIND row.dept_ids AS dept_id
            MATCH (d:Department {id: dept_id})
            MERGE (p)-[:BELONGS_TO]->(d)
            """,
            "Узлы: Person:Itmo (+ связи с Department)",
            transform_func=transform_person_itmo
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM persons_external",
            """
            UNWIND $batch AS row
            MERGE (p:Person:External {id: row.id})
            SET p.name_en = row.name_en,
                p.name_variants = row.name_variants,
                p.email = row.email
            """,
            "Узлы: Person:External",
            transform_func=lambda r: {**r, 'name_variants': safe_json_loads(r['name_variants'])}
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM repositories",
            """
            UNWIND $batch AS row
            MERGE (r:Repository {id: row.id})
            SET r.name = row.name,
                r.url = row.url,
                r.description = row.description,
                r.access_date = date(row.access_date),
                r.has_readme = (row.has_readme = 1),
                r.stars_num = toInteger(row.stars_num),
                r.last_updated = date(row.last_updated),
                r.license = row.license,
                r.contributors = row.contributors

            // Создаем владельца (GitHubProfile), если его нет, и вяжем с репо
            WITH r, row
            WHERE row.owner IS NOT NULL
            MERGE (gh:GitHubProfile {login: row.owner})
            ON CREATE SET gh.type = row.owner_type
            MERGE (r)-[:OWNED_BY]->(gh)
            """,
            "Узлы: Repository (+ владельцы GitHubProfile)",
            transform_func=lambda r: {**r, 'contributors': safe_json_loads(r['contributors'])}
        )

        # ==========================================
        # 2. ЗАГРУЗКА СВЯЗЕЙ (RELATIONSHIPS)
        # ==========================================

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT repository_id, department_id FROM repository_departments",
            """
            UNWIND $batch AS row
            MATCH (r:Repository {id: row.repository_id})
            MATCH (d:Department {id: row.department_id})
            MERGE (r)-[:DEVELOPED_BY]->(d)
            """,
            "Связи: Repository -[:DEVELOPED_BY]-> Department"
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT repository_id, publication_id FROM repository_publications",
            """
            UNWIND $batch AS row
            MATCH (r:Repository {id: row.repository_id})
            MATCH (p:Publication {id: row.publication_id})
            MERGE (r)-[:IMPLEMENTS]->(p)
            """,
            "Связи: Repository -[:IMPLEMENTS]-> Publication"
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM repository_persons",
            """
            UNWIND $batch AS row
            MATCH (r:Repository {id: row.repository_id})
            MATCH (p:Person:Itmo {id: row.person_id})
            MERGE (p)-[rel:CONTRIBUTED_TO]->(r)
            SET rel.role = row.role
            """,
            "Связи: Person:Itmo -[:CONTRIBUTED_TO]-> Repository"
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT publication_id, department_id FROM publication_departments",
            """
            UNWIND $batch AS row
            MATCH (pub:Publication {id: row.publication_id})
            MATCH (dep:Department {id: row.department_id})
            MERGE (pub)-[:PRODUCED_BY]->(dep)
            """,
            "Связи: Publication -[:PRODUCED_BY]-> Department"
        )

        fetch_and_load(
            sqlite_conn, neo4j_driver,
            "SELECT * FROM publication_authors",
            """
            UNWIND $batch AS row
            MATCH (p:Person {id: row.person_id})
            MATCH (pub:Publication {id: row.publication_id})
            MERGE (p)-[rel:AUTHORED]->(pub)
            SET rel.position = toInteger(row.author_position),
                rel.affiliation = row.affiliation_string,
                rel.is_corresponding = (row.is_corresponding = 1)
            """,
            "Связи: Person -[:AUTHORED]-> Publication"
        )

        # ==========================================
        # 3. LLM-НАХОДКИ (MENTIONS_LINK)
        # ==========================================

        # А) Сопоставленные ссылки: ведем прямо на Repository
        fetch_and_load(
            sqlite_conn, neo4j_driver,
            """
            SELECT rl.publication_id, r.url as repository_url,
                   rl.context, rl.page_number, rl.is_relevant,
                   rl.llm_confidence, rl.llm_reason
            FROM repo_links rl
            JOIN repositories r ON rl.url = r.url
            """,
            """
            UNWIND $batch AS row
            MATCH (p:Publication {id: row.publication_id})
            MATCH (r:Repository {url: row.repository_url})
            MERGE (p)-[rel:MENTIONS_LINK]->(r)
            SET rel.context = row.context,
                rel.page_number = toInteger(row.page_number),
                rel.is_relevant = (row.is_relevant = 1),
                rel.llm_confidence = toFloat(row.llm_confidence),
                rel.llm_reason = row.llm_reason
            """,
            "Связи: Publication -[:MENTIONS_LINK]-> Repository (Сопоставленные)"
        )

        # Б) Несопоставленные ссылки: создаем узел LinkCandidate и ведем связь к нему
        fetch_and_load(
            sqlite_conn, neo4j_driver,
            """
            SELECT rl.id, rl.publication_id, rl.url as link_url, rl.host,
                   rl.context, rl.page_number, rl.is_relevant,
                   rl.llm_confidence, rl.llm_reason
            FROM repo_links rl
            WHERE rl.url NOT IN (SELECT url FROM repositories)
            """,
            """
            UNWIND $batch AS row
            MATCH (p:Publication {id: row.publication_id})
            MERGE (lc:LinkCandidate {id: row.id})
            SET lc.url = row.link_url,
                lc.host = row.host

            MERGE (p)-[rel:MENTIONS_LINK]->(lc)
            SET rel.context = row.context,
                rel.page_number = toInteger(row.page_number),
                rel.is_relevant = (row.is_relevant = 1),
                rel.llm_confidence = toFloat(row.llm_confidence),
                rel.llm_reason = row.llm_reason
            """,
            "Узлы и Связи: Publication -[:MENTIONS_LINK]-> LinkCandidate (Несопоставленные)"
        )

    finally:
        sqlite_conn.close()
        neo4j_driver.close()
        print("\nМиграция успешно завершена!")


if __name__ == "__main__":
    run_migration()
