# Brainbox — Knowledge Platform

Self-hosted, AI-native engineering knowledge platform (Django + PostgreSQL).
A `terv.md` architektúra **Phase 1 (Core)** megvalósítása, a TrueNAS-os
deploymenthez igazított build/pull folyamattal.

> **Fájl a source of truth.** A tudás a lemezen lévő fájlokban él, a PostgreSQL
> metaadatot, ACL-t, verzió-indexet és auditot tárol. A REST API és a jelenlegi
> UI minden művelete a központi `PermissionService`-en megy keresztül; a
> vector/search réteg (Phase 3) később sem kerülheti meg.

---

## Mi van kész (Phase 1 + 2)

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
- **Docker** image GitHub Actions-ből (GHCR), TrueNAS compose pull-al.
- **CI**: ruff + Django check + migration check + tests (PostgreSQL service).

Nem része ennek a körnek (a terv Phase 3–7): Qdrant/embedding, MCP server,
Secret Vault, OIDC, haladó AI.

---

## Architektúra röviden

```text
Web UI / REST API / (MCP később)
        │
        ▼
Application services  (apps/*/services.py)
        │
        ▼
PermissionService     (apps/permissions/services.py)
        │
        ▼
Resource ACL · Storage adapter (LocalFilesystem)
```

Minden írás/olvasás a service rétegen és a permission engine-en megy át.
A `StorageAdapter` (`apps/resources/storage.py`) mögött később Git- vagy
S3-alapú implementáció tehető anélkül, hogy az üzleti logika változna.

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
links/  git/  users/  groups/  permissions/  api-keys/  audit/
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
  audit/                AuditEvent (+ AuditService)
  api/                  DRF serializers/viewsets/urls/health
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

## Következő lépések (a terv szerint)

Phase 3 full-text + Qdrant + embedding · Phase 4 MCP server · Phase 5 Secret Vault ·
Phase 6 OIDC/enterprise · Phase 7 advanced AI.

Phase 2 maradék finomítás (opcionális): valódi PR nyitás a GitHub API-val,
webhook-alapú pull, ütemezett háttér-sync a `worker`-ben.
