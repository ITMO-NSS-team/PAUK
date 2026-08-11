# Слой хранения MongoDB (raw + prepared)

Подробности `PreparedStore`/`RawStore` — крупным планом; вход в этот слой
(кто и зачем читает/пишет) — см. [`pipeline-flow.md`](pipeline-flow.md),
здесь он показан кратко. Прозаическое описание — [`../architecture/storage.md`](../architecture/storage.md).

```mermaid
flowchart TB
    IN["вход: pauk collect / normalize / enrich[stage]<br/>(подробно — pipeline-flow.md)"]
    IN --> DB

    subgraph DB["MongoDB: settings.mongo_db"]
        direction TB

        subgraph RAWSTORE["RawStore — коллекция raw, append-only"]
            direction TB
            RDOC["документ:<br/>{ source, group, fetched_at, request, payload }"]
            RAPP["append(source, payload, request)<br/>→ insert_one, всегда новый документ"]
            RREAD["read(source)<br/>→ find({source, groups: self.group})<br/>.sort(fetched_at, 1)"]
            RCROSS["кросс-групповой скан (напр. collect_raw_orcids)<br/>find({source}) — без фильтра по group,<br/>последний fetched_at побеждает"]
            RAPP -.->|"insert_one"| RDOC
            RREAD -.->|"find, своя группа"| RDOC
            RCROSS -.->|"find, все группы"| RDOC
        end

        subgraph PREPSTORE["PreparedStore — 6 коллекций, глобальные сущности"]
            direction TB

            subgraph COLLS["publications · persons · departments ·<br/>repositories · github_profiles · repo_links"]
                direction LR
                PDOC["документ:<br/>_id = id (у repo_links — publication_id)<br/>...поля модели...<br/>groups: [group_a, group_b, ...]"]
            end

            subgraph READS["чтение — два разных доступа"]
                direction TB
                GROUPREAD["read_rows / read_models(entity)<br/>find({groups: self.group})<br/>«всё, что видела моя группа»<br/>— так читают enrichment-стадии"]
                IDREAD["get_rows / get_models(entity, ids)<br/>find({_id: {$in: ids}})<br/>без фильтра по группе<br/>— так normalize ищет уже обогащённую<br/>сущность из другой, пересекающейся группы"]
            end

            subgraph WRITES["write_rows / write_models(entity, rows)<br/>задаёт полное состояние группы для entity"]
                direction TB
                WSTEP1["1. на каждую row:<br/>update_one({_id: row_id},<br/>{$set: row, $addToSet: {groups: self.group}},<br/>upsert=True)"]
                WSTEP2["2. update_many({groups: self.group,<br/>_id: {$nin: written_ids}},<br/>{$pull: {groups: self.group}})<br/>группа отзывает claim на то,<br/>что не переподтвердила в этом вызове"]
                WSTEP3["3. delete_many({groups: {$size: 0}})<br/>документ без единой группы<br/>больше не достижим — удаляется"]
                WSTEP1 --> WSTEP2 --> WSTEP3
            end

            GROUPREAD -.-> PDOC
            IDREAD -.-> PDOC
            WSTEP1 -.-> PDOC
        end
    end

    DB --> OUT["выход: pauk publish graph → Neo4j<br/>(подробно — pipeline-flow.md)"]
```

## Почему два способа чтения

`read_rows`/`read_models` — рабочий набор своей группы, им пользуются
все стадии `enrich` без исключения: они видят и переписывают только то,
что уже отмечено их группой.

`get_rows`/`get_models` — точечный лукап по id, без разбора по группам.
Единственный сегодняшний потребитель —
`OpenAlexNormalizer._seed` (`pauk/pipeline/normalize.py`): раз
сущности глобальные, работа, уже сделанная над этим id **другой**
группой, не должна теряться только потому, что текущая группа видит
его впервые.

## Почему запись — не просто upsert

Каждая стадия по контракту читает **весь** рабочий набор своей группы
для сущности, мутирует, пишет **весь** набор обратно — тот же контракт,
что раньше был у перезаписи файла целиком. Шаги 2–3 воспроизводят
именно это: если строка была в рабочем наборе группы, но в этот раз не
переподтверждена (свёрнута dedup-стадией, переименована при
ренормализации), запись группы на неё снимается, а документ, оставшийся
без единой группы, удаляется — иначе он повис бы в коллекции
недостижимым мусором.

## Ключ документа не всегда `id`

`_id` — значение поля-ключа сущности. Для всех сущностей это `id`,
кроме `repo_links`: у `RepoLink` нет собственного `id`, ключ —
`publication_id` (одна строка на публикацию, список ссылок внутри).
