# `departments` — стейдж

**Что здесь:** как публикация и её авторы получают привязку к
департаменту.

**Какие файлы задействует:** `pauk/pipeline/stages/departments.py`.

Для каждого `Person` собирает весь текст его заявленных аффилиаций
(`" ".join(a.affiliation or "" for a in person.authored)`, `casefold`), и
матчит департаменты из `StaticStore` против него: департамент подходит,
если его `name_en` **или любой** из `name_variants` — подстрока этого
текста.

```python
matched = [d.id for d in departments
           if d.name_en.casefold() in text or any(v.casefold() in text for v in d.name_variants)]
```

Найденные id пишутся и на `Person.department_ids`, и (для ИТМО-персон) на
`department_ids` каждой публикации, которую человек написал.
