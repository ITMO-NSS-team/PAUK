# Деплой на лабораторный сервер

**Что здесь:** как карта разворачивается на сервере лаборатории и как
подключается внешний PDF-Crawler-Service.

**Какие файлы задействует:** `scripts/deploy.sh`, `.env`/`.env.example`
(`PAUK_PDF_CRAWLER_URL`).

Карта крутится на `einsteinium.nsslab` (`ssh asteb@einsteinium.nsslab`,
доступ по SSH-ключу, доступ по VPN лаборатории): статика из `~/pauk-gui/`
раздаётся `python3 -m http.server` в `screen`-сессии `pauk`, порт по
умолчанию **8501** — `http://einsteinium.nsslab:8501`.

Своего юнита systemd у раздачи нет: `screen`-сессия не переживает
перезагрузку сервера, после неё карту нужно поднимать заново — `deploy.sh`.

## MongoDB

Контейнер `pauk-mongo` (образ `mongo:4.4` — сервер на старом Xeon без
AVX, `mongo:5+` требует AVX и падает с `Illegal instruction`), поднят
вручную (не через `scripts/deploy.sh`, тот его не трогает):

```bash
docker run -d --name pauk-mongo -p 27017:27017 \
  -v /home/asteb/pauk-mongo-data:/data/db mongo:4.4
```

Данные — bind-mount на `/home/asteb/pauk-mongo-data` на диске сервера
(не docker volume — реальная папка, видна снаружи контейнера, растёт
с объёмом raw/prepared-данных). Порт `27017` пробрасывается наружу без
аутентификации — доступ ограничен только VPN лаборатории, как у Neo4j.
Проверить состояние: `docker ps -a | grep mongo`, точный mount:
`docker inspect pauk-mongo --format '{{json .Mounts}}' | python3 -m json.tool`.

## `scripts/deploy.sh`

```bash
./scripts/deploy.sh
PORT=8503 ./scripts/deploy.sh   # если 8501 занят
```

На сервере ничего не собирается и `git pull` не делается — сайт
статический, собирается локально с текущей ветки:

1. Если есть незакоммиченные изменения — предупреждение и подтверждение
   (`y/N`): они попадут в сборку.
2. Проверка, что в `data/gui/private/` есть все четыре JSON (`graph-data`,
   `authors-detail`, `repos-detail`, `pubs-detail`). Сами данные скрипт не
   генерирует — это `python -m pauk.gui.graph_builder` заранее.
3. `npm run build` в `pauk/gui/web/` — Vite копирует данные из
   `data/gui/private/` в `dist/`, поэтому на сервер уходит **приватный**
   вариант, с личными полями авторов. Доступ к серверу — только через VPN
   лаборатории.
4. `ping` до хоста — явная проверка VPN, а не невнятный таймаут SSH позже;
   `ssh -o ConnectTimeout=5 ... true` — проверка, что ключ принимается.
5. `rsync -avz --delete pauk/gui/web/dist/ → ~/pauk-gui/`.
6. Перезапуск `screen`-сессии `pauk` (`python3 -m http.server`) через
   `ssh ... bash -l <<EOF` (**логин-шелл**, чтобы подхватились
   `.bashrc`/`.profile`). Существующая сессия с тем же именем гасится
   явной проверкой `screen -list | grep -q '\.pauk[[:space:]]'` — без неё
   `quit` на несуществующую сессию печатает шумное "No screen session
   found.". После запуска — пауза и повторная проверка, что сессия
   поднялась; если нет, скрипт печатает хвост `~/pauk.log` на сервере
   (например, `Address already in use`).

`python3 -m http.server` не сжимает ответы: первая загрузка страницы —
около 36 МБ JSON. Повторные заходы дешевле — сервер отвечает `304` на
неизменённые файлы.

## PDF-Crawler-Service — не часть этого репозитория

Отдельный сервис ([github.com/gurinboru/PDF-Crawler-Service](https://github.com/gurinboru/PDF-Crawler-Service),
FastAPI+Redis+Celery) — опциональный fallback для `code_links.py`, когда
у публикации нет `pdf_url` (см. [pipeline/code-links.md](pipeline/code-links.md)).
Развёртывание, поддержка и корректность самого поиска PDF — не в зоне
ответственности этого репозитория; здесь только клиентская сторона
интеграции.

**Конфигурация** (`.env`, см. `.env.example`):

```
PAUK_PDF_CRAWLER_URL=http://localhost:8000/api/v1
```

Пусто по умолчанию — фолбэк выключен целиком, пока не задан явно.

Клиентская часть шлёт `GET {url}/health` (без ретраев, раз за прогон) и
`GET {url}/download?url=https://doi.org/<doi>` — форма запросов взята из
README сервиса. Расхождение с реальным API проявится как обычный `FAILED`
у стейджа `code_links` с текстом ошибки, не тихой порчей данных.
