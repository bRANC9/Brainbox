# Brainbox — Knowledge Platform

Self-hosted, AI-native engineering knowledge platform (Django + PostgreSQL).

> **Fájl a source of truth.** A tudás a lemezen lévő fájlokban él; a PostgreSQL
> metaadatot, ACL-t, verzió-indexet és auditot tárol. Minden művelet (REST, Web
> UI, MCP) a központi `PermissionService`-en megy keresztül — a keresés és a
> vector store sem kerülheti meg.

---

## Főbb képességek

- **Workspace / Project / Mappa / Dokumentum / Fájl** — minden objektum egy
  `Resource` (UUID, szülőlánc, önálló ACL). A workspace lehet `shared` vagy
  `personal` (utóbbi kizárólag a tulajdonosé, nem megosztható).
- **Permission engine**: explicit ALLOW/DENY, öröklés felfelé-lefelé a fában,
  deny precedence, csoport-támogatás, API key scope szűkítés.
- **Verziótörténet**: dokumentum és fájl snapshot + diff + restore.
- **Címkék (tags)**: first-class tagging dokun, mappán, fájlon, projektben.
- **Keresés**: full-text + semantic + hybrid, chunkolás + embedding
  (offline `deterministic` default, vagy OpenAI-kompatibilis / Ollama provider),
  opcionális Qdrant vector store. Permission filtering a retrieval **után**.
- **Git integráció**: workspace/project csatolható repóhoz, checkout a source of
  truth; pull → import, platform írás → auto-commit/push
  (`direct_commit` / `branch_pr`). **Obsidian vault import** (frontmatter +
  `[[wikilink]]` → `ResourceLink`).
- **MCP szerver**: JSON-RPC 2.0 a `/mcp` endpointen, ~30 permission-aware tool.
- **Secret Vault**: user-owned, Fernet-titkosított secret, használat-auditalva,
  redacting secret scanner.
- **Advanced AI**: skill/pattern/convention/decision/example discovery,
  link-gráf alapú related knowledge, AI draft — minden AI-szülés **DRAFT**,
  emberi jóváhagyással lesz approved.
- **Határidők**: a fájlokból kinyerve (frontmatter + szöveg), naptár / agenda /
  **iCal feed**, permission-aware.
- **Háttérfeladatok**: DB-alapú scheduler + worker (cron/interval/daily…,
  lease-lock, retry, dedupe) — Celery/Redis nélkül.
- **Egyéb**: opcionális OIDC/SSO, audit log, Prometheus `/metrics`,
  `/healthz` + `/readyz`, runtime settings a web UI-ról.

---

## Architektúra

```text
Web UI / REST API / MCP          (apps/web, apps/api, apps/mcp)
        ▼
Application services             (apps/*/services.py)
        ▼
PermissionService                (apps/permissions/services.py)
        ▼
Resource ACL · Storage adapter   (LocalFilesystem | Git checkout)
        ▼
Search / Chunking / Vector store (apps/search, apps/embeddings)
```

Minden írás/olvasás a service rétegen és a permission engine-en megy át.

---

## Deploy (TrueNAS — pull, nem build)

A Docker image-et a GitHub Actions buildeli GHCR-be
(`ghcr.io/branc9/brainbox:latest|<sha>|v<x.y.z>`); a TrueNAS csak pullol.
A `web`/`worker`/`scheduler` konténereken Watchtower label van, tehát host-oldali
Watchtower-rel automatikusan frissülnek.

> GHCR csomag láthatósága: az első build után **privát** — vagy nyilvánosra
> váltod a GitHub-on (Packages → Change visibility), vagy a TrueNAS hoston
> `docker login ghcr.io` (PAT `read:packages`).

```bash
cp .env.example .env    # kötelező: DJANGO_SECRET_KEY, POSTGRES_PASSWORD
docker compose up -d
```

Szolgáltatások: `db` (Postgres 16), `web` (gunicorn, `${BRAINBOX_PORT:-8000}`),
`worker` (job queue), `scheduler` (DB lease, csak egy tickel). Opcionális:
`qdrant` (`--profile vector` + `QDRANT_URL=http://qdrant:6333`).
Reprodukálható deploy: `BRAINBOX_TAG=v1.0.0` a `latest` helyett.
Adatok: `pgdata`, `knowledge_data` named volume-ok.

Első bejelentkezés: `/accounts/login/`; admin: `/admin/`. Superuser bootkor:
`BRAINBOX_ADMIN_USERNAME` / `BRAINBOX_ADMIN_PASSWORD`.

---

## Lokális fejlesztés

```bash
# Docker Compose (build): http://localhost:8000 (admin/admin, lásd .env)
docker compose -f docker-compose.dev.yml up --build

# Tesztek Dockerben — ugyanaz a Postgres, mint a CI-ben (--build kötelező!)
docker compose -f docker-compose.dev.yml run --rm --build tests
docker compose -f docker-compose.dev.yml run --rm --build tests \
    python manage.py test apps.web -v 2

# Közvetlen futás (SQLite fallback, ha nincs DATABASE_URL)
python -m venv .venv && . .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
python manage.py migrate
python manage.py bootstrap --username admin --email admin@example.com --password admin
python manage.py runserver
```

Lint / migráció-ellenőrzés:

```bash
ruff check .
python manage.py makemigrations --check --dry-run
```

---

## API és MCP

Autentikáció: session (böngésző) vagy API key: `Authorization: ApiKey <kulcs>`
(vagy `X-API-Key`). Kulcsmenedzselés **csak sessionnel** — API kulccsal új kulcs
nem hozható létre.

Főbb végpontok (`/api/v1/`):

```text
workspaces/ projects/ resources/ documents/ files/ folders/ links/
git/ secrets/ users/ groups/ permissions/ api-keys/ audit/
search/ discovery/ quality/ drafts/ jobs/ deadlines/ settings/
```

- `GET /llm` — önleíró manifest (böngészőnek HTML, agentnek JSON): hitelesítés,
  teljes végpontlista jogosultságokkal, MCP toolok sémával, domain modell,
  konvenciók. Nyilvános, hogy agent kulcs előtt elolvashassa.
- `POST /mcp` — JSON-RPC 2.0 (MCP kliensek: Claude Code, OpenCode, Codex…).
  Minden tool permission-aware és auditált (`MCP_REQUEST`).

---

## Jogosultságok röviden

| Szint | Alapértelmezés |
|---|---|
| Workspace (shared) | privát, amíg meg nem osztják; a jog dinamikusan öröklődik |
| Workspace (personal) | csak a tulajdonos; nem Share-olható, nem adható át |
| Project / Mappa / Doksi / Fájl | a konténer ACL-jét örökli, önálló ACL is lehet |

- Négy jogszint szigorúan beágyazott: `read ⊂ write ⊂ delete ⊂ admin`.
- A **DENY abszolút**: bármelyik ős szinten blokkol, a szűkebb ALLOW fölé megy.
- Az API key csak **szűkíteni** tud (scope), bővíteni nem.
- **Superuser bypass nincs**: a rendszeradmin nem látja a tartalmat; az egyetlen
  út az auditált *jogosultság-átvétel*, amit a tulajdonos `no_takeover`-rel
  letilthat. Vészhelyzeti kapcsoló: `BRAINBOX_SUPERUSER_BYPASS=1` (átállás előtt
  `manage.py access_audit`).
- ⚠️ Az ACL csak a **Knowledge groups**-ot használja (`/manage/groups/`); a
  Django admin "Groups" nem számít az ACL-ben.

Hasznos parancsok: `manage.py access_audit`, `backfill_owners`,
`ensure_personal_workspaces`.

---

## Kulcs fontosságú beállítások (env)

```text
DJANGO_SECRET_KEY, POSTGRES_PASSWORD        # kötelező
BRAINBOX_SECRET_KEY                         # Fernet kulcs — élesben állítsd be!
                                            # (kulcscsere a meglévő secreteket olvashatatlanná teszi)
BRAINBOX_GIT_TOKEN                          # shared PAT privát repókhoz (repo-szintű Vault secret felülírhatja)
BRAINBOX_EMBEDDING_PROVIDER                 # deterministic (default) | ollama | openai
BRAINBOX_LLM_PROVIDER                       # noop | openai | ollama
QDRANT_URL                                  # üres = beépített local vector store
BRAINBOX_METRICS_TOKEN                      # /metrics Bearer-token
OIDC_ENABLED + OIDC_*                       # opcionális SSO
```

Az AI/embedding/keresés/Git/job értékek **a web UI-ról** is állíthatók
(`/manage/settings/`, superuser) — a DB-ben élő felülírás verzi az env-et, nem
kell újraindítás. Provider ellenőrzés: `manage.py provider_check`,
áraindexelés: `manage.py reindex`.

---

## Könyvtárszerkezet

```text
config/                 Django projekt (settings/urls/wsgi/asgi)
apps/
  accounts/             User, ApiKey + scope, API key auth
  groups/               Group, GroupMembership (Knowledge groups)
  resources/            Resource identity + storage adapter + worker
  permissions/          ResourceACL + PermissionService
  workspaces/           Workspace (shared/personal), Project, owner/transfer
  documents/ files/     Dokumentum/fájl + verziótörténet, frontmatter
  links/                ResourceLink (permission-aware)
  tags/                 First-class címkék (Taggable mixin)
  git/                  GitRepository, GitClient, GitService (pull/commit/push)
  jobs/                 DB scheduler: registry, schedule math, engine, worker
  deadlines/            Határidő-kinyerés, naptár/agenda/iCal
  settings_store/       RuntimeSetting — UI-kezelhető felülírások
  embeddings/ search/   Chunkolás, embedding providerek, vector store, reranker
  knowledge/            Graph, AI draft, discovery, quality
  secrets/              Secret Vault (Fernet, scanner)
  mcp/                  JSON-RPC MCP szerver + tool registry
  audit/                AuditEvent
  monitoring/           Prometheus domain gauge-ök + /readyz
  api/ web/             DRF + Web UI
templates/ static/      UI
.github/workflows/      build-push.yml (GHCR), ci.yml
docker-compose.yml      prod/TrueNAS (pull) · dev.yml (build) · truenas.yml
terv.md                 eredeti architektúra terv
```
