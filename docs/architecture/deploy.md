# Deploying to the lab server

**What it covers:** how the map is deployed to the lab server, and how the
external PDF-Crawler-Service is wired in.

**Files:** `scripts/deploy.sh`, `scripts/serve_static.py`,
`scripts/mongo-up.sh`, `.env`/`.env.example` (`PAUK_PDF_CRAWLER_URL`).

The map runs on `einsteinium.nsslab` (`ssh asteb@einsteinium.nsslab`; access
by SSH key, and only over the lab VPN). Static files from `~/pauk-gui/` are
served by `~/serve_static.py` (`scripts/serve_static.py`) in a `screen`
session named `pauk`, on port **8501** by default:
`http://einsteinium.nsslab:8501`.

The static server has no systemd unit. A `screen` session does not survive a
server reboot, so after one the map must be brought back with `deploy.sh`.
(The admin panel and worker do have units; see
[../admin-panel.md](../admin-panel.md).)

## MongoDB

The `pauk-mongo` container runs `mongo:4.4`: the server has an old Xeon
without AVX, and `mongo:5+` requires AVX and crashes with `Illegal
instruction`. It is managed by `scripts/mongo-up.sh`, not by `deploy.sh`.
The script is idempotent: it creates the container if missing, starts it if
stopped, and sets `--restart unless-stopped`. Run it from a machine on the
VPN. The equivalent manual command:

```bash
docker run -d --name pauk-mongo --restart unless-stopped -p 27017:27017 \
  -v /home/asteb/pauk-mongo-data:/data/db mongo:4.4
```

Data is a bind mount at `/home/asteb/pauk-mongo-data` on the server's disk
(not a docker volume): a real folder, visible outside the container, that
grows with the raw and prepared data. Port `27017` is published without
authentication; access is limited only by the lab VPN, as with Neo4j. Check
the state with `docker ps -a | grep mongo`, and the exact mount with
`docker inspect pauk-mongo --format '{{json .Mounts}}' | python3 -m json.tool`.

## `scripts/deploy.sh`

```bash
./scripts/deploy.sh
PORT=8503 ./scripts/deploy.sh   # if 8501 is taken
```

Nothing is built on the server and no `git pull` happens there. The site is
static and is built locally from the current branch:

1. If there are uncommitted changes, the script warns and asks to confirm
   (`y/N`): they will end up in the build.
2. It checks that `data/gui/private/` holds all four JSON files
   (`graph-data`, `authors-detail`, `repos-detail`, `pubs-detail`). It does
   not generate them; run `pauk gui build` first.
3. `npm run build` in `pauk/gui/web/`. Vite copies the data from
   `data/gui/private/` into `dist/`, so the **private** variant, with
   personal author fields, is what reaches the server. Access to the server
   is only through the lab VPN.
4. `ping` to the host, as an explicit VPN check instead of an obscure SSH
   timeout later; then `ssh -o ConnectTimeout=5 ... true` to check that the
   key is accepted.
5. `rsync -avz --delete pauk/gui/web/dist/` to `~/pauk-gui/`, and
   `scripts/serve_static.py` to `~/serve_static.py` (next to the site
   folder, not inside it, so the script itself is not served).
6. The `screen` session `pauk` (`python3 ~/serve_static.py <port>`) is
   restarted over `ssh ... bash -l <<EOF`. It must be a **login shell** so
   that `.bashrc`/`.profile` are loaded. An existing session of the same name
   is killed only after an explicit `screen -list | grep -q
   '\.pauk[[:space:]]'` check; without it, `quit` on a missing session prints
   a noisy "No screen session found.". After starting, the script pauses and
   checks again that the session is up; if not, it prints the tail of
   `~/pauk.log` on the server (for example `Address already in use`).

Between the build and `ping` the script runs `gzip -k` on the JSON, JS, CSS
and HTML files in `dist/`. `serve_static.py` is the standard-library
`http.server` with one addition: if the browser accepts gzip and a `.gz`
file sits next to the requested one, it sends that with `Content-Encoding:
gzip`. Site data shrinks roughly five times, which matters most for
`graph-data.json`, the file that blocks the first render. PDFs and images are
not compressed again. Repeat visits get `304` for unchanged files. The
script has no dependencies and uses no syntax newer than Python 3.6, because
the server's python3 version is not pinned.

## PDF-Crawler-Service: not part of this repository

A separate service
([github.com/gurinboru/PDF-Crawler-Service](https://github.com/gurinboru/PDF-Crawler-Service),
FastAPI + Redis + Celery) is an optional fallback for `code_links.py`, tried
after the publication's own `pdf_urls` (see
[pipeline/code-links.md](pipeline/code-links.md)). Deploying and maintaining
it, and the correctness of its PDF search, are outside this repository; only
the client side of the integration lives here.

**Configuration** (`.env`, see `.env.example`):

```
PAUK_PDF_CRAWLER_URL=http://localhost:8000/api/v1
```

Empty by default: the fallback is off entirely until this is set.

The client sends `GET {url}/health` (no retries, once per run) and
`GET {url}/download?url=<doi>` (with the DOI as the `url` value, no retries,
180 s timeout), following the service's README. A mismatch with the real API
shows up as an ordinary `FAILED` on the `code_links` stage with the error
text, not as silent data corruption.
