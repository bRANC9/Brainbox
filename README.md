# Brainbox — Knowledge Platform

Self-hosted, AI-native engineering knowledge platform (Django + PostgreSQL).
A `terv.md` architektúra **Phase 1 (Core)** megvalósítása, a TrueNAS-os
deploymenthez igazított build/pull folyamattal.

> **Fájl a source of truth.** A tudás a lemezen lévő fájlokban él, a PostgreSQL
> metaadatot, ACL-t, verzió-indexet és auditot tárol. A REST API és a jelenlegi
> UI minden művelete a központi `PermissionService`-en megy keresztül; a
> vector/search réteg (Phase 3) később sem kerülheti meg.

---

## Mi van kész (Phase 1)

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
- **Docker** image GitHub Actions-ből (GHCR), TrueNAS compose pull-al.
- **CI**: ruff + Django check + migration check + tests (PostgreSQL service).

Nem része ennek a körnek (a terv Phase 2–7): Git integráció, Obsidian import,
Qdrant/embedding, MCP server, Secret Vault, OIDC.

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
links/  users/  groups/  permissions/  api-keys/  audit/
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

Phase 2 Git adapter + Obsidian import · Phase 3 full-text + Qdrant + embedding ·
Phase 4 MCP server · Phase 5 Secret Vault · Phase 6 OIDC/enterprise · Phase 7 advanced AI.
