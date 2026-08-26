# Схема графа Neo4j

```mermaid
classDiagram
    class Person {
        +id : OpenAlex author ID
        +is_itmo : bool
        +openalex_id
        +orcid
        +name_en
        +name_variants
        +email
        +first_name_ru
        +second_name_ru
        +surname_ru
        +degree
        +github
        +google_scholar
        +openreview
        +thesis
        +other_names
        +homepage
        +linkedin
        +affiliations : JSON
        +merged_ids
        ~ остальные экспериментальные поля
        ~ не заполняются, см. models.md
    }

    class Organization {
        +id : name_en slug (uid)
        +name_en : unique
        +name_ru
        +ror_id
        +country
        +type
    }

    class Department {
        +id : name_en slug (uid)
        +name_en
        +name_ru
        +name_variants
        +kind : megafaculty|faculty|institute|center|department|lab
        +parent_id : uid родителя-Department
        +organization_id : uid Organization (верхний уровень)
    }

    class Publication {
        +id : OpenAlex work ID
        +title
        +type
        +fields
        +journal
        +doi
        +publication_date
        +year
        +has_code
        +code_url
        +funding : JSON
        +openalex_url
        +pdf_url
        +abstract
        +versions : JSON
        +merged_ids
        +full_text
    }

    class Repository {
        +id : github_owner_name
        +name
        +url : unique
        +github_id
        +cited_urls
        +description
        +access_date
        +has_readme
        +stars_num
        +last_updated
        +license
        +contributors
        +merged_ids
    }

    class GitHubProfile {
        +id
        +login : unique
        +name
        +html_url
        +description
        +location
        +company
        +type
    }

    class LinkCandidate {
        +id : сам URL
        +url
        +host
    }

    Person --> Department : BELONGS_TO
    Person --> Publication : AUTHORED
    Person --> Repository : CONTRIBUTED_TO
    Publication --> Department : PRODUCED_BY
    Publication --> Repository : MENTIONS_LINK
    Publication --> LinkCandidate : MENTIONS_LINK
    Repository --> Department : DEVELOPED_BY
    Repository --> Publication : IMPLEMENTS
    Repository --> GitHubProfile : OWNED_BY
    Department --> Department : PART_OF
    Department --> Organization : PART_OF
```

`AUTHORED` несёт `position`/`affiliation`/`affiliation_source`/
`is_corresponding`; `CONTRIBUTED_TO` — `role` (`owner` или `contributor`),
её строит стадия `github_match`; `MENTIONS_LINK` — `context`
(список), `page_number` (список, `0` = абстракт), `is_relevant`,
`llm_confidence`, `llm_reason`. Подробности и уникальные ключи — в
[`../architecture/neo4j-graph.md`](../architecture/neo4j-graph.md).

Иерархия подразделений рекурсивна: подразделение `PART_OF` своего родителя —
либо другого `Department` (`parent_id`), либо корневой `Organization`
(`organization_id`); задано ровно одно из двух. Несколько организаций (ИТМО и
со-аффилиации) сосуществуют в одном графе как разные корни.

Почты и имена из коммитов, которые собирает матчер (`GitHubProfile.emails`,
`commit_names`, `repos`, `Person.emails`), в граф не публикуются: это
доказательства, на которых он строит решение, а не факты об аккаунте —
и это адреса живых людей. Они остаются в prepared JSONL.
