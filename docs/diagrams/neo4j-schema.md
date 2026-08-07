# Схема графа Neo4j

```mermaid
classDiagram
    class Person {
        <<Person:Itmo | Person:External>>
        +id : OpenAlex author ID
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
        +affiliations : JSON
        +merged_ids
        ~ 20 экспериментальных полей — не заполняются,
        ~ см. models.md
    }

    class Department {
        +id : sha256(name_en)
        +name_en
        +name_ru
        +name_variants
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
```

`AUTHORED` несёт `position`/`affiliation`/`affiliation_source`/
`is_corresponding`; `CONTRIBUTED_TO` — `role`; `MENTIONS_LINK` — `context`
(список), `page_number` (список, `0` = абстракт), `is_relevant`,
`llm_confidence`, `llm_reason`. Подробности и уникальные ключи — в
[`../architecture/neo4j-graph.md`](../architecture/neo4j-graph.md).
