# Brainbox — Knowledge Platform

Self-hosted, AI-native engineering knowledge platform (Django + PostgreSQL).
A `terv.md` architektúra **teljes MVP-ja (Phase 1–7)**, TrueNAS-os
deploymenthez igazított build/pull folyamattal.

> **Fájl a source of truth.** A tudás a lemezen lévő fájlokban él, a PostgreSQL
> metaadatot, ACL-t, verzió-indexet és auditot tárol. A REST API, a Web UI és az
> MCP szerver minden művelete a központi `PermissionService`-en megy keresztül —
> a keresés és a vector store sem kerülheti meg.

---

## Mi van kész (Phase 1–7)

- **Resource Identity**: minden objektum egy `Resource` (UUID, `parent` lánc).
- **Workspace / Project** létrehozás, listázás, ACL.
- **User / Group / GroupMembership** (Django auth + saját csoportok).
- **Permission engine**: explicit ALLOW/DENY, öröklés workspace → project → document,
  deny precedence, group támogatás, API key scope szűkítés.
- **Document** + **DocumentVersion** (fájl a lemezen, DB snapshot history, diff, restore),
  YAML frontmatter feldolgozás.
- **File** + **FileVersion** (bináris/tetszőleges fájl, upload/download).
- **ResourceLink** (dokumentum/projekt/workspace közötti linkek, permission-aware).
- **API key**: HMAC-SHA256 hash, prefix, revoke, expiry, scope (workspace/project szint).
- **Audit log**: append-only `AuditEvent`.
- **REST API** (`/api/v1/...`) és **Web UI** (Django templates + Markdown render).
- **Git integráció (Phase 2)**: repository csatolása Workspace/Project szinten,
  a checkout a source of truth (storage adapter), pull → import, platform írás →
  auto-commit (+ push), `direct_commit` / `branch_pr` workflow, branch/commit/diff.
- **Obsidian import**: meglévő vault Git repóból, frontmatter + `[[wikilink]]` →
  `ResourceLink`, nem-markdown fájlok → `File` (checksum/verzió).
- **Search (Phase 3)**: full-text + semantic + hybrid keresés, chunkolás, embedding
  (offline `deterministic` vagy OpenAI-kompatibilis provider), vector store
  (beépített local, opcionális **Qdrant** REST), **permission filtering a retrieval
  után**, approved/priority ranking, `reindex` parancs.
- **MCP server (Phase 4)**: JSON-RPC 2.0 `/mcp` endpoint, ~30 permission-aware tool
  (keresés, lekérés, lista, link-követés, írás, git, secret, discovery).
- **Secret Vault (Phase 5)**: user-owned, titkosított (Fernet) secret, workspace/project
  attachment, használat-auditalva (érték soha nem kerül logba), redacting secret scanner.
- **Enterprise/auth (Phase 6)**: opcionális OIDC/SSO login, permission/API key/group/audit
  kezelő UI.
- **Advanced AI (Phase 7)**: skill/pattern/convention/decision/example discovery,
  link-gráf alapú „related knowledge", AI draft generálás (minden AI által létrehozott
  tudás **DRAFT**), human approve/reject workflow, knowledge quality metrikák.

A terv **Definition of Done** tételeinek megfelelően az első verzió használható;
a Qdrant/embedding külső szolgáltatás opcionális (beépített local store a default).

---

## Architektúra röviden

```text
Web UI / REST API / MCP  (apps/web, apps/api, apps/mcp)
        │
        ▼
Application services  (apps/*/services.py)
        │
        ▼
PermissionService     (apps/permissions/services.py)
        │
        ▼
Resource ACL · Storage adapter (LocalFilesystem | Git checkout)
        │
        ▼
Search / Chunking / Vector store (apps/search, apps/embeddings)
```

Minden írás/olvasás a service rétegen és a permission engine-en megy át.
A `StorageAdapter` (`apps/resources/storage.py`) mögött később S3-alapú
implementáció tehető anélkül, hogy az üzleti logika változna.

---

## Deployment TrueNAS-on (pull, nem build)

A Docker image-et **a GitHub Actions buildeli** és feltolja a GHCR-be:

```text
ghcr.io/branc9/brainbox:latest        # main branch
ghcr.io/branc9/brainbox:<sha->        # minden commit
ghcr.io/branc9/brainbox:v1.2.3        # git tag
```

A TrueNAS csak a `docker-compose.yml`-t olvassa és pull-ol. **Nem buildel.**

> **Fontos – GHCR láthatóság.** Az első build után a csomag alapból **privát**,
> így a TrueNAS nem tudja hitelesítés nélkül pullolni. Két lehetőség:
>
> - **A) Csomagot nyilvánosra tenni:** GitHub → jobb felső avatar → *Your packages*
>   → `brainbox` → *Package settings* → *Change visibility* → **Public**.
> - **B) Hitelesítés a TrueNAS hoston** (privát marad):
>   ```bash
>   echo "<PAT read:packages scope-pal>" | docker login ghcr.io -u branc9 --password-stdin
>   ```
>   (Classic PAT: `read:packages`. Finomhangolt token: *Packages → Read*.)

1. Másold a `.env.example`-t `.env`-be és töltsd ki (kötelező:
   `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`).
2. (Opcionális) állítsd be `BRAINBOX_ADMIN_USERNAME` / `BRAINBOX_ADMIN_PASSWORD`-ot,
   hogy az első induláskor létrejöjjön a superuser.
3. Indítás:

```bash
docker compose up -d
```

Ez elindítja: `db` (Postgres), `web` (gunicorn), `worker` (idle háttér-loop).
Opcionálisan a `qdrant` szolgáltatás is (`--profile vector`, lásd a keresés fejezetet).

- A `web` a `${BRAINBOX_PORT:-8000}` portra publikál; a Pangolin reverse proxy
  erre/pro erre a konténerre irányítson.
- Reprodukálható deploy: állítsd a `BRAINBOX_TAG`-et konkrét tagre (pl. `v1.0.0`)
  a `latest` helyett.
- Adatok: `pgdata`, `knowledge_data`, `static_data` named volume-okban.
  Ha TrueNAS datasetre kötöd, cseréld a `knowledge_data` mountot bind mountra,
  és figyelj a jogosultságokra (a konténer UID 1000-ként fut).

Első bejelentkezés: `http://<host>:${BRAINBOX_PORT}/` → `/accounts/login/`.
Admin: `/admin/`.

---

## Lokális fejlesztés

### Docker Compose (build)

```bash
docker compose -f docker-compose.dev.yml up --build
# http://localhost:8000  (admin/admin, lásd .env)
```

### Közvetlenül (SQLite fallback)

```bash
python -m venv .venv && . .venv/Scripts/activate    # Windows
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                                # töltsd ki
python manage.py migrate
python manage.py bootstrap --username admin --email admin@example.com --password admin
python manage.py runserver
```

`DATABASE_URL` nélkül SQLite-ot használ (fejlesztéshez). Dockerben mindig Postgres.

### Tesztek / lint

```bash
python manage.py test
ruff check .
python manage.py makemigrations --check --dry-run
```

---

## API

Autentikáció:

- **Session** (böngésző / admin), vagy
- **API key**: `Authorization: ApiKey <kulcs>` vagy `X-API-Key: <kulcs>`.

Főbb végpontok (`/api/v1/`):

```text
workspaces/  projects/  resources/  documents/  files/
links/  git/  secrets/  users/  groups/  permissions/  api-keys/  audit/
search/  discovery/  quality/  drafts/
```

Néhány hasznos művelet:

```text
GET    /api/v1/documents/<id>/versions/                 # verziólista
GET    /api/v1/documents/<id>/versions/<n>/diff?to=<m>  # diff
POST   /api/v1/documents/<id>/restore/                  # visszaállítás
POST   /api/v1/files/upload/                            # multipart feltöltés
GET    /api/v1/files/<id>/download/
POST   /api/v1/api-keys/                                # a nyers kulcs csak itt jelenik meg
POST   /api/v1/api-keys/<id>/revoke/
```

Példa API key létrehozásra (scope: csak a Company workspace olvasható):

```bash
curl -X POST https://brainbox.example.com/api/v1/api-keys/ \
  -H "Authorization: ApiKey <admin-kulcs>" -H "Content-Type: application/json" \
  -d '{"name":"Claude - Company","scopes":[
        {"workspace":"<company-uuid>","permission":"read","effect":"allow"}]}'
```

---

## Git integráció / Obsidian import (Phase 2)

Egy Workspace vagy Project Git-backed lehet. Ilyenkor a **repository checkout a
source of truth**: a dokumentumok/fájlok közvetlenül a repó fájába íródnak, és a
platform minden írása vissza-commitol a Gitbe (`auto_sync`), a beállított
workflow szerint.

- `direct_commit` — közvetlenül a `default_branch`-re commitol és pushol.
- `branch_pr` — `brainbox/<user>` feature branchre commitol és pushol (PR-t
  külsőleg nyitsz).

Csatolás és Obsidian vault import (clone + scan egy lépésben):

```bash
# Csatolás workspace szinten (privát repónál a BRAINBOX_GIT_TOKEN-t használja)
curl -X POST https://brainbox.example.com/api/v1/git/ \
  -H "Authorization: ApiKey <kulcs>" -H "Content-Type: application/json" \
  -d '{"workspace":"<workspace-uuid>","name":"Company Vault",
       "remote_url":"https://github.com/acme/knowledge.git",
       "default_branch":"main","workflow":"branch_pr"}'

# Csatolás project szinten
#   {"project":"<project-uuid>", ...}  (workspace is kötelező)
```

Git műveletek repónként (`<repo-id>` = a `git/` erőforrás id-ja):

```text
GET    /api/v1/git/                      # lista (permission-szűrve)
GET    /api/v1/git/<id>/status/          # branch, head, dirty, utolsó commitok
POST   /api/v1/git/<id>/pull/            # fetch + rebase + import (scan)
POST   /api/v1/git/<id>/scan/            # working tree újraolvasása DB-be
POST   /api/v1/git/<id>/commit/          # {"message": "..."}  add -A + commit
POST   /api/v1/git/<id>/push/            # aktuális branch push
GET    /api/v1/git/<id>/branches/
GET    /api/v1/git/<id>/diff/?from=<ref>&to=<ref>
```

Web UI-n a Workspace/Project oldalon Git panel mutatja az állapotot és az utolsó
commitokat; a **Sync from Git** gomb `pull` + import a `require_write` joggal.

Import részletek: `.md`/`.markdown` → **Document** (frontmatter → metadata,
status, priority; heading/fájlnév → cím), minden más fájl → **File**
(checksum-alapú verzió). Az Obsidian `[[wikilink]]` és relatív `[..](x.md)`
hivatkozások feloldódnak (path / basename / cím alapján) és **ResourceLink**-ké
válnak. A `.git`, `.obsidian`, `node_modules` és dotfile-ok kimaradnak.

> Git-backed repóhoz tartozó fájlok a `knowledge_data` volume `git/<repo-id>/`
> könyvtárában élnek. Nem Git-backed workspace/projekt a korábbi
> `workspaces/<id>/...` layoutot használja — a storage adapter ezt elrejti.

---

## Keresés, chunkolás, embedding (Phase 3)

Minden dokumentum íráskor automatikusan chunkokra bomlik és embeddinget kap
(`BRAINBOX_AUTO_INDEX=0`-val kikapcsolható, utólag: `python manage.py reindex`).

```text
GET /api/v1/search/?q=azure+bicep&mode=hybrid&workspace=<uuid>&limit=10
```

- `mode=text` – DB-független contains keresés a chunkokon;
- `mode=semantic` – embedding-alapú koszinusz hasonlóság;
- `mode=hybrid` – a kettő kombinációja (default).
- `BRAINBOX_SEARCH_BACKEND=postgres` – PostgreSQL full-text (`SearchVector/SearchRank`).

**Permission filtering a retrieval után kötelező**: a vector találat önmagában nem
ad hozzáférést. A ranking az `approved` státuszt és a `priority` mezőt is súlyozza.

Embedding provider: `BRAINBOX_EMBEDDING_PROVIDER=deterministic` (offline, nulla
függőség — default) vagy `ollama` / `openai` (OpenAI-kompatibilis `/v1/embeddings`).

**Lokális Ollama** (a Docker hoston futó modell-szerver):

```bash
# .env
BRAINBOX_EMBEDDING_PROVIDER=ollama
OPENAI_BASE_URL=http://host.docker.internal:11434/v1   # konténerből nem localhost!
BRAINBOX_EMBEDDING_MODEL=nomic-embed-text              # dimenzió: 768
BRAINBOX_EMBEDDING_DIM=768                             # a mért érték
BRAINBOX_LLM_PROVIDER=ollama
BRAINBOX_LLM_MODEL=llama3.1
# OPENAI_API_KEY üresen hagyható – az Ollama figyelmen kívül hagyja

python manage.py provider_check    # elérhetőség + mért dimenzió ellenőrzése
python manage.py reindex           # áraindexelés az új modellel
```

A `compose` a `web` és `worker` service-nek ad `host.docker.internal:host-gateway`
mappinget, hogy TrueNAS-on/Linuxon is elérje a hoszton futó Ollamát. A Qdrant
collection méretét az indexelés a **mért** vektorhosszból állítja be, így a
dimenzió-eltérés nem töri el a tárolást.

Vector store: `QDRANT_URL` üres = beépített local store (embedding a DB-ben);
`QDRANT_URL=http://qdrant:6333` + `docker compose --profile vector up -d` = Qdrant
(Qdrant **REST** API-n, extra Python csomag nélkül).

---

## MCP szerver (Phase 4)

JSON-RPC 2.0 over HTTP POST: `POST /mcp` (a `GET /mcp` a szerver infókat adja).
Hitelesítés API key-jel (`Authorization: ApiKey <key>` vagy `X-API-Key`).

Támogatott metódusok: `initialize`, `notifications/initialized`, `ping`,
`tools/list`, `tools/call` (batch is).

Példa:

```bash
curl -s https://brainbox.example.com/mcp \
  -H "Authorization: ApiKey $BRAINBOX_KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
        "name":"knowledge_search","arguments":{"query":"azure bicep","limit":5}}}'
```

Tool-kATEGÓRIák: `knowledge_search` / `knowledge_get` / `knowledge_get_summary` /
`knowledge_list_*` / `knowledge_get_skill|pattern|convention|decision|example` /
`knowledge_follow_link` / `knowledge_create|update|delete_document` /
`knowledge_create|update_project` / `knowledge_get_git_status` /
`knowledge_create_branch|commit|pull_request` / `secret_list|get_metadata|use` /
`knowledge_discover|related|generate_draft|approve_document|quality_metrics`.

Minden tool a `PermissionService`-en megy át, és a hívás `MCP_REQUEST` audit eseményt
 generál. Claude Code / OpenCode / Codex stb. MCP klienssel használható.

---

## Secret Vault (Phase 5)

A secret **user-owned**, a value titkosítva tárolódik (Fernet; kulcs:
`BRAINBOX_SECRET_KEY`, hiányában `DJANGO_SECRET_KEY` — élesben mindenképpen
állítsd be a `BRAINBOX_SECRET_KEY`-t, mert a kulcscsere a meglévő secret-eket
olvashatatlanná teszi).

```text
POST   /api/v1/secrets/                 # létrehozás (payload csak egyszer látszik)
POST   /api/v1/secrets/<id>/rotate/     # érték cseréje
POST   /api/v1/secrets/<id>/attach/     # {"workspace": "<uuid>"} vagy {"project": "..."}
POST   /api/v1/secrets/<id>/detach/
POST   /api/v1/secrets/<id>/use/        # érték visszaadása (auditált)
```

A `use` csak a tulajdonosnak működik, és csak ott, ahová a secret fel van
csatolva (vagy ahol nincs attachment → bárhol). A secret **értéke soha nem kerül
audit logba**. `BRAINBOX_SECRET_SCAN_MODE=warn|reject` esetén a dokumentum írás
ellenőrzi, tartalmaz-e valószínű credentialt (`manage.py scan_secrets`).

---

## Advanced AI (Phase 7)

- **Discovery** (`/discover/`, `GET /api/v1/discovery/`): skill/pattern/convention/
  decision/example dokumentumok, approved-előnye, permission-szűrve.
- **Related knowledge**: link-gráf BFS (`GET /api/v1/documents/<id>/related/?depth=2`),
  a dokumentum oldalon is látszik; inaccessible szomszédnál „restricted" jelzés.
- **AI draft** (`POST /api/v1/drafts/`): prompt + releváns céges tudás kontextusban;
  a provider abstraction (`BRAINBOX_LLM_PROVIDER=noop|openai`). Az AI-generált
  dokumentum **mindig DRAFT** státuszú, emberi jóváhagyásig.
- **Approve / Reject**: web gombok + `POST /api/v1/documents/<id>/approve|reject/`
  + MCP `knowledge_approve_document`; státuszváltás auditált.
- **Quality** (`GET /api/v1/quality/`, staff): státusz-mix, approved arány, orphan
  (link nélküli) és stale (> `BRAINBOX_STALE_DAYS`) dokumentumok.

---

## Háttérfeladatok (DB-alapú scheduler, ai-handler minta)

A `apps/jobs` egy **DB-alapú ütemező + worker**, ami az ai-handler scheduler
mintáját portolja – de a saját dokumentált korlátainak **javításával**. Nincs
Celery/Redis: a `jobs_job_run` sor maga a queue, amit a workerek atomikusan
claimelnek.

```text
scheduler konténer ──lease──▶ tick() ──▶ waiting run sorok ──▶ worker konténer(ek)
                                   (skip-if-busy, retry, orphan recovery)
```

- **Ütemezés**: `once` / `interval` / `hourly` / `daily` / `weekly` / `monthly` / **`cron`**,
  `schedule_config` JSON + opcionális `{"timezone": "Europe/Budapest"}` (a minta ezt még
  hiányzónak jelölte). A `cron` típus a `croniter`-t használja (alap függőség).
- **Lock**: DB-lease (`jobs_scheduler_state`) heartbeat-tel – több replika esetén
  is csak egy scheduler tickel (a minta /tmp lock fájlt használt, skálázásnál
  cserélni kellett volna).
- **Retry**: hibánál újrapróbálkozás `max_retries` szerint, `attempt` növelésével.
- **Idempotencia**: `dedupe_key` mező – azonos payload-hoz tartozó aktív run mellé
  nem szabadul új run enqueue-elni.
- **Recover**: age-aware orphan-recovery a beragadt waiting/running run-okra.
- **Beépített taskok** (`apps/jobs/tasks_brainbox.py`): `embedding_backfill`,
  `git_sync_all`, `recover_stuck_runs`, `prune_job_history`, `prune_audit_log`.

Kezelés:

```bash
python manage.py jobctl --list-tasks
python manage.py jobctl --status
python manage.py jobctl --run git_sync_all --run-now
python manage.py jobctl --tick
python manage.py jobctl --runs
```

REST (`/api/v1/jobs/`, staff): `run_now`, `registry`, `scheduler_status`, `runs`;
`/api/v1/job-runs/<id>/cancel/`. Adminban: *Jobs* és *Job runs*.

> **pip-csomag lehetőség:** a `apps/jobs` magja (registry, schedule-matematika,
> modellek, engine, admin, API) **framework-only**, nincs benne Brainbox-specifikus
> import → szinte 1:1 kiemelhető önálló Django csomaggá. Az `apps/jobs/pyproject.toml`
> már a kivitelhez kész (név `brainbox-jobs`, opcionális `[cron]` extra). A Brainbox-függő
> taskok külön modulban vannak, így kivétel nélkül. Kivitelkor: a `src/brainbox_jobs/`
> útvonalra másolás + a brainbox-specifikus taskok kihagyása.

---

## Monitoring (Prometheus)

- `GET /metrics` – Prometheus scrape (django-prometheus + Brainbox domain gauge-ök:
  dokumentumok státuszonként, chunk-ek, embedding-index, git repók/sync állapot,
  secretek, job run-ok, audit események). `BRAINBOX_METRICS_TOKEN` esetén
  Bearer-token kell.
- `GET /healthz` – liveness (DB ping).
- `GET /readyz` – readiness (DB + migrációk alkalmazva + storage írható).

---

## Reranking

A keresési pipeline `retrieval → permission filter → rerank → top-N`. A reranker
provider (terv 20):
- `heuristic` (default) – determinisztikus, külső szolgáltatás nélkül: title/path
  egyezés, pontos frázis, tudás-státusz, frissesség;
- `crossencoder` – OpenAI-kompatibilis `/rerank` endpoint (opcionális);
- `none` – csak alap score.

---

## Könyvtárszerkezet


```text
config/                 Django projekt (settings/urls/wsgi/asgi)
apps/
  accounts/             User, ApiKey, ApiKeyScope, API key auth
  groups/               Group, GroupMembership
  resources/            Resource identity + storage adapter + worker
  permissions/          ResourceACL + PermissionService (constants)
  workspaces/           Workspace, Project (+ services)
  documents/            Document, DocumentVersion, frontmatter, services
  files/                File, FileVersion (+ services)
  links/                ResourceLink (+ LinkService)
  git/                  GitRepository, GitSyncState, GitCommitReference, GitClient, GitService
  jobs/                 DB-backed scheduler: registry, schedule math, Job/JobRun, engine, worker
  monitoring/           Prometheus domain collector + /readyz
  embeddings/           KnowledgeChunk, EmbeddingIndexState, providers, chunking, vector stores
  search/               SearchService (text + semantic + hybrid) + rerankers
  knowledge/            graph, AI drafts, discovery, quality metrics (+ LLM provider)
  secrets/              Secret, SecretAttachment, crypto, scanner
  mcp/                  JSON-RPC MCP szerver + tool registry
  audit/                AuditEvent (+ AuditService)
  api/                  DRF serializers/viewsets/urls/search/health
  web/                  Web UI views
templates/              Django templates
static/                 CSS
.github/workflows/      build-push.yml (GHCR), ci.yml
docker-compose.yml      TrueNAS / prod (pull)
docker-compose.dev.yml  local (build)
Dockerfile entrypoint.sh
terv.md                 eredeti architektúra terv
```

---

## Következő lépések

A terv mind a 7 fázisa megvan. Célszerű következő lépések üzemeltetési oldalon:

1. **TrueNAS deploy** – GHCR csomag nyilvánossá tétele (vagy PAT), `.env` kitöltése,
   `docker compose up -d`, első bejelentkezés.
2. **Opcionális Qdrant**: `QDRANT_URL=http://qdrant:6333` + `docker compose --profile
   vector up -d`, majd `python manage.py reindex`.
3. **SSO bekapcsolás**: `OIDC_ENABLED=1` + issuer/client adatok.
4. **Valódi PR nyitás**: `BRAINBOX_GITHUB_TOKEN` beállítása (MCP
   `knowledge_create_pull_request`).
5. **Ütemezett Git sync**: a `worker` szolgáltatás jelenleg idle loop; a
   `requeue/sync` feladat ütemezése a következő iteráció.
6. Embedding/keresés finomhangolása valós modellel (`BRAINBOX_EMBEDDING_PROVIDER=openai`).
