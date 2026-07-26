from .models import (
    Department,
    ExternalPerson,
    GitHubProfile,
    ItmoPerson,
    LinkCandidate,
    Publication,
    Repository,
)


def _link_authored(tx, person_id, authored):
    """(Person)-[:AUTHORED {position, affiliation, is_corresponding}]->(Publication).
    Общее для :Itmo и :External"""
    for a in authored:
        tx.run(
            "MATCH (n:Person {id:$pid}) "
            "MERGE (pub:Publication {id:$pub}) "
            "MERGE (n)-[r:AUTHORED]->(pub) "
            "SET r.position=$position, r.affiliation=$affiliation, "
            "    r.is_corresponding=$is_corresponding",
            pid=person_id, pub=a.publication_id, position=a.position,
            affiliation=a.affiliation, is_corresponding=a.is_corresponding,
        )


def load_department(tx, p: Department):
    tx.run(
        "MERGE (n:Department {id:$id}) "
        "SET n.name_en=$name_en, n.name_ru=$name_ru, n.name_variants=$name_variants",
        p.model_dump(),
    )


def load_github_profile(tx, p: GitHubProfile):
    tx.run(
        "MERGE (n:GitHubProfile {id:$id}) "
        "SET n.login=$login, n.name=$name, n.html_url=$html_url, "
        "    n.description=$description, n.location=$location, n.type=$type",
        p.model_dump(),
    )


def load_link_candidate(tx, p: LinkCandidate):
    tx.run(
        "MERGE (n:LinkCandidate {id:$id}) SET n.url=$url, n.host=$host",
        p.model_dump(),
    )


def load_repository(tx, p: Repository):
    tx.run(
        "MERGE (n:Repository {id:$id}) "
        "SET n.name=$name, n.url=$url, n.description=$description, "
        "    n.access_date=$access_date, n.has_readme=$has_readme, "
        "    n.stars_num=$stars_num, n.last_updated=$last_updated, "
        "    n.license=$license, n.contributors=$contributors",
        p.model_dump(),
    )
    if p.owner_login:                                   # -> OWNED_BY
        tx.run(
            "MATCH (n:Repository {id:$id}) "
            "MERGE (gh:GitHubProfile {login:$login}) "
            "MERGE (n)-[:OWNED_BY]->(gh)",
            id=p.id, login=p.owner_login,
        )
    for did in p.department_ids:                        # -> DEVELOPED_BY
        tx.run(
            "MATCH (n:Repository {id:$id}) MERGE (d:Department {id:$did}) "
            "MERGE (n)-[:DEVELOPED_BY]->(d)",
            id=p.id, did=did,
        )
    for pub_id in p.publication_ids:                    # -> IMPLEMENTS
        tx.run(
            "MATCH (n:Repository {id:$id}) MERGE (pub:Publication {id:$pub}) "
            "MERGE (n)-[:IMPLEMENTS]->(pub)",
            id=p.id, pub=pub_id,
        )


def load_publication(tx, p: Publication):
    tx.run(
        "MERGE (n:Publication {id:$id}) "
        "SET n.title=$title, n.journal=$journal, n.doi=$doi, "
        "    n.publication_date=$publication_date, n.year=$year, "
        "    n.has_code=$has_code, n.code_url=$code_url, n.funding=$funding, "
        "    n.openalex_url=$openalex_url, n.pdf_url=$pdf_url, n.abstract=$abstract",
        p.model_dump(),
    )
    for did in p.department_ids:                        # -> PRODUCED_BY
        tx.run(
            "MATCH (n:Publication {id:$id}) MERGE (d:Department {id:$did}) "
            "MERGE (n)-[:PRODUCED_BY]->(d)",
            id=p.id, did=did,
        )
    for m in p.mentions_links:                          # -> MENTIONS_LINK (репо или кандидат)
        merge_target, target_key = (
            ("MERGE (t:Repository {url:$key})", m.repository_url)
            if m.target_kind == "repository"
            else ("MERGE (t:LinkCandidate {id:$key})", m.candidate_id)
        )
        tx.run(
            "MATCH (n:Publication {id:$id}) " + merge_target + " "
            "MERGE (n)-[r:MENTIONS_LINK]->(t) "
            "SET r.context=$context, r.page_number=$page_number, "
            "    r.is_relevant=$is_relevant, r.llm_confidence=$llm_confidence, "
            "    r.llm_reason=$llm_reason",
            id=p.id, key=target_key, context=m.context, page_number=m.page_number,
            is_relevant=m.is_relevant, llm_confidence=m.llm_confidence,
            llm_reason=m.llm_reason,
        )


def load_itmo_person(tx, p: ItmoPerson):
    tx.run(
        "MERGE (n:Person:Itmo {id:$id}) "
        "SET n.name_en=$name_en, n.name_variants=$name_variants, n.email=$email, "
        "    n.first_name_ru=$first_name_ru, n.second_name_ru=$second_name_ru, "
        "    n.surname_ru=$surname_ru, n.degree=$degree, n.github=$github, "
        "    n.google_scholar=$google_scholar, n.openreview=$openreview, "
        "    n.thesis=$thesis, n.created_at=$created_at",
        p.model_dump(),
    )
    for did in p.department_ids:                        # -> BELONGS_TO
        tx.run(
            "MATCH (n:Person:Itmo {id:$id}) MERGE (d:Department {id:$did}) "
            "MERGE (n)-[:BELONGS_TO]->(d)",
            id=p.id, did=did,
        )
    _link_authored(tx, p.id, p.authored)
    for c in p.contributed_to:                          # -> CONTRIBUTED_TO
        tx.run(
            "MATCH (n:Person:Itmo {id:$id}) MERGE (rep:Repository {id:$rid}) "
            "MERGE (n)-[r:CONTRIBUTED_TO]->(rep) SET r.role=$role",
            id=p.id, rid=c.repository_id, role=c.role,
        )


def load_external_person(tx, p: ExternalPerson):
    tx.run(
        "MERGE (n:Person:External {id:$id}) "
        "SET n.name_en=$name_en, n.name_variants=$name_variants, n.email=$email",
        p.model_dump(),
    )
    _link_authored(tx, p.id, p.authored)