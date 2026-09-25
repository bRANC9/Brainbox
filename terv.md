# Knowledge Platform — terv.md

## 1. Cél

Egy self-hosted, AI-native engineering knowledge platform fejlesztése, amely a vállalat és a felhasználók tudását strukturáltan, jogosultságokkal, verziózva és AI-agentek számára közvetlenül használható módon kezeli.

A rendszer fő célja nem egy klasszikus wiki létrehozása, hanem egy központi **organizational memory / engineering memory**:

> „Nézd meg a céges tudástárban, hogyan szoktuk ezt megoldani.”

Az AI agent a válasz vagy kód generálása előtt képes legyen a releváns céges skill-eket, konvenciókat, patternöket, döntéseket és referenciafájlokat megkeresni, majd azokat követni.

A rendszernek támogatnia kell:
- személyes, családi, céges, HomeLab és tetszőleges további workspace-eket;
- workspace-eket és azokon belüli projecteket;
- dokumentumokat és tetszőleges fájlokat;
- Markdownot és meglévő Obsidian vaultok importját;
- opcionális Git repositoryt workspace vagy project szinten;
- több felhasználót és user groupokat;
- részletes ACL/permission kezelést;
- workspace/project/document/file szintű hozzáférést;
- workspace-ek és projectek közötti linkelést permission checkkel;
- opcionális publikus metadata/summary láthatóságot;
- REST API-t és MCP-t;
- felhasználónként több, külön scope-olható API/MCP kulcsot;
- teljes audit logot;
- dokumentumverziózást;
- embeddinget, chunkolást és semantic search/RAG-ot;
- személyhez tartozó Secret Vaultot, amelyből credentialek workspace/project/harness számára csatolhatók;
- OIDC/OAuth és klasszikus Django authenticationt;
- Dockeres self-hosted deploymentet;
- Pangolin reverse proxy mögötti működést.

---

# 2. Alapelvek

## 2.1 Source of truth

A tényleges tudás **fájlban van**, nem a PostgreSQL-ben.

```text
Markdown / code / config / binary file
            =
      SOURCE OF TRUTH
```

A PostgreSQL:
- metadata;
- ACL;
- user/group;
- resource identity;
- linkek;
- verzió-index;
- Git állapot;
- audit;
- search/index állapot;
- secret metadata;
- egyéb gyorsan lekérdezhető információ.

A Qdrant vagy más vector store:
- embedding;
- chunk;
- semantic retrieval index.

A keresési index és embedding mindig újraépíthető a source-of-truth fájlokból.

## 2.2 Permission first

Sem a REST API, sem az MCP, sem a semantic search, sem a Git integration nem kerülheti meg a permission engine-t.

Különösen:

```text
search
  -> candidate results
  -> permission filtering
  -> allowed results
  -> LLM context
```

A vector database találata önmagában soha nem jelent hozzáférést.

## 2.3 AI nem kerülheti meg az ACL-t

MCP-n keresztül sem lehet olyan dokumentumot olvasni, amelyhez az adott user/API key/harness nem rendelkezik megfelelő jogosultsággal.

## 2.4 A tudás minősége számít

A rendszer különböztesse meg például:

- draft;
- experimental;
- approved;
- deprecated;
- archived.

Az AI retrieval preferálja az approved tudást, de a rendszer ne törölje a régi vagy kísérleti információt.

---

# 3. Fogalmi modell

## 3.1 Resource Identity

Minden kezelhető objektum egységes resource modellre épül.

Resource típusok:

```text
Workspace
Project
Document
File
GitRepository
Secret
MCPServer
APIKey
```

Minden resource rendelkezik stabil azonosítóval.

Javasolt:

```text
UUID / ULID
```

A permission engine ne külön-külön legyen implementálva document/project/workspace szinten, hanem közös Resource ACL mechanizmust használjon.

---

# 4. Workspace

A workspace szabadon létrehozható.

Nincs hardcode-olt:

```text
PERSONAL
FAMILY
COMPANY
HOMELAB
```

típus.

Ezek csak tipikus példák.

Például:

```text
Personal
Family
Company
HomeLab
MENTA
Freelance
Research
```

A workspace tartalmazhat közvetlenül dokumentumokat/fájlokat és projecteket is.

```text
Workspace
├── documents/
├── files/
├── Project A/
└── Project B/
```

---

# 5. Project

A project egy workspace-en belüli önálló resource.

```text
Company
├── Azure
├── MENTA
└── Internal
```

A project:
- saját dokumentumokat/fájlokat tartalmazhat;
- saját Git repositoryt kapcsolhat;
- saját ACL-t kaphat;
- saját secret attachmenteket használhat;
- saját AI/RAG scope-pal rendelkezhet;
- más projectekkel és workspace-ekkel linkelhet.

A workspace közvetlenül is tartalmazhat tudást, tehát a project nem kötelező.

---

# 6. File / Document modell

A rendszer ne csak Markdownot támogasson.

Támogatott legyen gyakorlatilag bármilyen fájl:

```text
.md
.yaml
.yml
.json
.bicep
.py
.ts
.js
.sql
.sh
.ps1
.txt
.pdf
.png
.jpg
.zip
...
```

A rendszer MIME type és file metadata alapján kezelje őket.

A Markdown különösen fontos, mert:
- ember által olvasható;
- Git-barát;
- Obsidian-kompatibilis;
- AI számára jól feldolgozható.

---

# 7. Knowledge structure

Javasolt céges engineering knowledge struktúra:

```text
company/
├── skills/
│   ├── azure-bicep/
│   │   ├── SKILL.md
│   │   ├── conventions.md
│   │   ├── examples/
│   │   │   ├── container-app.bicep
│   │   │   ├── postgres.bicep
│   │   │   └── networking.bicep
│   │   └── decisions/
│   │
│   ├── docker/
│   ├── fastapi/
│   └── azure-devops/
│
├── patterns/
├── conventions/
├── decisions/
└── examples/
```

A rendszernek nem kell ezt a struktúrát kötelezővé tennie. Ez csak ajánlott szervezési minta.

---

# 8. Knowledge metadata

Markdown/file mellett DB metadata legyen.

Példa frontmatter:

```yaml
---
type: skill
domain: azure
status: approved
priority: 100
owner: engineering
tags:
  - azure
  - bicep
  - deployment
---
```

Metadata DB-ben is legyen indexelve.

Fontos mezők:

```text
type
status
priority
owner
tags
created_at
updated_at
author
source
workspace
project
```

A DB metadata nem írhatja felül a fájl tartalmát.

---

# 9. Summary / restricted metadata

Egy resource külön kezelje:

```text
title
summary
content
metadata
```

Lehetséges:

```text
title   -> látható
summary -> látható
content -> restricted
```

Példa:

```text
HomeLab / TrueNAS / Overview

Summary:
A helyi TrueNAS infrastruktúra Docker alapú szolgáltatásokat,
Home Assistantot és AI infrastruktúrát futtat.

Content:
RESTRICTED
```

Így egy céges AI tudhatja, hogy létezik a HomeLab TrueNAS dokumentáció, de a teljes tartalmat nem olvashatja.

A summary is ACL/visibility szabályok szerint legyen elérhető.

---

# 10. Link rendszer

A Markdown linkek mellett a rendszer DB-ben is tárolja a linkeket.

```text
ResourceLink
----------------
source_resource
target_resource
link_type
created_at
```

Támogatni kell:

```text
document -> document
project -> document
workspace -> document
project -> project
workspace -> project
workspace -> workspace
```

Cross-workspace link megengedett.

Példa:

```text
Company/Azure/deployment.md
        |
        +----> HomeLab/TrueNAS/Overview.md
```

A link létezhet attól függetlenül, hogy a user látja-e a cél resource tartalmát.

`follow_link` előtt mindig permission check történik.

Ha nincs hozzáférés:

```text
Target exists
Target summary may be visible
Target content is inaccessible
```

---

# 11. Permission model

ACL + inheritance.

Alapértelmezett hierarchia:

```text
Workspace
    ↓
Project
    ↓
Document/File
```

Permissionek:

```text
ALLOW
DENY
INHERITED
```

Legalább:

```text
READ
WRITE
DELETE
ADMIN
USE
```

A tényleges action permission resource-típustól függhet.

Például:
- Secret -> USE
- Git repo -> READ / WRITE / ADMIN
- API key -> USE / ADMIN
- Document -> READ / WRITE / DELETE
- Workspace -> READ / WRITE / ADMIN

Javasolt feloldási logika:

```text
explicit resource permission
        ↓
parent resource permission
        ↓
group/user permission
        ↓
default deny
```

Explicit DENY-nek legyen megfelelő prioritása.

A pontos konfliktusfeloldási algoritmust centralizált PermissionService kezelje, ne az egyes endpointok.

---

# 12. User és Group

User:
- saját workspace/project hozzáférések;
- group tagság;
- saját API/MCP kulcsok;
- saját Secret Vault.

Group:
- több userhez rendelhető;
- resource ACL-ben szerepelhet;
- workspace/project/document/file szinten használható.

Példa:

```text
Engineering
DevOps
MENTA
Management
Family
```

User lehet több group tagja.

---

# 13. API/MCP key

Egy user több API/MCP kulcsot hozhat létre.

Például:

```text
Claude - Company
Claude - Personal
HomeLab Agent
CI Agent
OpenCode
```

A key saját scope-pal rendelkezik.

Effective permission:

```text
User permissions
        +
API key scope
        =
Effective permissions
```

Az API key soha nem kaphat nagyobb jogot, mint a létrehozó user.

Példa:

```text
User:
  Company -> READ/WRITE
  Personal -> READ/WRITE

Key:
  Company -> READ
  Personal -> DENY
```

Ezzel egy céges Claude kulcs csak a céges tudást láthatja.

A kulcsok értéke csak egyszer jelenjen meg létrehozáskor, adatbázisban hash vagy biztonságos formában tárolandó.

---

# 14. MCP

A rendszer első osztályú MCP server legyen.

Javasolt toolok:

```text
knowledge_search
knowledge_get
knowledge_get_summary
knowledge_list_workspaces
knowledge_list_projects
knowledge_list_documents
knowledge_get_skill
knowledge_get_pattern
knowledge_get_convention
knowledge_get_decision
knowledge_get_example
knowledge_follow_link

knowledge_create_document
knowledge_update_document
knowledge_delete_document
knowledge_create_project
knowledge_update_project

knowledge_get_git_status
knowledge_create_branch
knowledge_create_commit
knowledge_create_pull_request
```

Secret oldalon:

```text
secret_list
secret_get_metadata
secret_use
```

A `secret_get` lehetőleg ne legyen szükséges a legtöbb harness számára. Az agent inkább `secret_use/inject` műveletet használjon.

Minden MCP tool a központi authorization layeren keresztül működjön.

---

# 15. REST API

A Django REST API legyen az MCP mögötti stabil backend contract.

Például:

```text
/api/v1/workspaces/
/api/v1/projects/
/api/v1/resources/
/api/v1/documents/
/api/v1/files/
/api/v1/search/
/api/v1/links/
/api/v1/users/
/api/v1/groups/
/api/v1/permissions/
/api/v1/api-keys/
/api/v1/secrets/
/api/v1/git/
/api/v1/audit/
```

Az MCP ne közvetlenül PostgreSQL-hez vagy filesystemhez férjen.

```text
MCP
 ↓
Knowledge application services
 ↓
Permission engine
 ↓
Storage
```

---

# 16. Git integration

Git opcionális resource.

Git repository kapcsolható:

```text
Workspace
```

vagy:

```text
Project
```

szinthez.

Példa:

```text
Company
└── Git repository

Company
└── Azure
    └── Git repository
```

Mindkettőt támogatni kell.

Meglévő Obsidian vault Git repositoryból importálható.

Import:

```text
Git repo
   ↓
workspace/project
   ↓
filesystem sync
   ↓
metadata scan
   ↓
chunking
   ↓
embedding
```

---

# 17. Git two-way sync

Git és Knowledge UI kétirányú legyen.

Git módosítás:

```text
git pull / webhook / scheduled sync
        ↓
detect changes
        ↓
update metadata
        ↓
update version
        ↓
re-index
```

Web UI/MCP módosítás:

```text
edit
 ↓
file update
 ↓
version
 ↓
Git commit (ha Git-backed resource)
 ↓
index update
```

Git-backed workspace/project esetén beállítható:

```text
direct commit
```

vagy:

```text
feature branch + review/PR
```

A Git workflow resource konfiguráció legyen.

---

# 18. Versioning

Minden módosítás verziózandó.

DB modellek:

```text
Document
DocumentVersion
FileVersion
```

Git-backed resource esetén a Git history is megmarad.

Nem Git-backed resource esetén a platform saját version historyt biztosít.

Web UI:

```text
Document
 ├── Edit
 ├── History
 ├── Compare
 └── Restore
```

Diff:

```text
Version 13 → Version 14

- old pattern
+ approved company pattern
```

AI által végzett módosítás is verzió legyen.

Minden versionhöz:
- user;
- API key;
- source;
- timestamp;
- change type;
- optional Git commit;
- audit reference.

---

# 19. Draft / Approval workflow

AI által létrehozott knowledge alapértelmezett státusza:

```text
DRAFT
```

Workflow:

```text
AI generated
    ↓
DRAFT
    ↓
Human review
    ↓
APPROVED
```

További állapotok:

```text
EXPERIMENTAL
DEPRECATED
ARCHIVED
```

Az AI retrieval preferálja az approved knowledge-t.

---

# 20. Search / RAG

A rendszer több keresési módot használjon:

```text
Full-text / BM25
Semantic / vector
Metadata filtering
Tag filtering
Graph/link traversal
```

Pipeline:

```text
User query
    ↓
candidate retrieval
    ├── full text
    ├── vector
    └── metadata
    ↓
reranking
    ↓
permission filtering
    ↓
knowledge priority
    ↓
LLM context
```

Fontos:

**permission filtering a retrieval után is kötelező.**

Javasolt vector store:

```text
Qdrant
```

de legyen adapteres, hogy később cserélhető legyen.

---

# 21. Chunk modell

Chunk metadata:

```text
document_id
resource_id
workspace_id
project_id
version_id
chunk_index
content
summary
embedding
status
priority
```

A chunk nem önálló permission source.

A hozzáférést mindig a parent resource ACL alapján kell ellenőrizni.

---

# 22. Knowledge priority

Metadata:

```yaml
status: approved
priority: 100
```

A retrieval preferálhatja:

1. approved company knowledge;
2. project-specific approved knowledge;
3. conventions/patterns;
4. experimental knowledge;
5. draft knowledge;
6. egyéb források.

Ez nem merev választási sorrend, hanem retrieval ranking signal.

---

# 23. Secret Vault

A Secret Vault **user-owned**, nem workspace-owned.

```text
User
└── Secret Vault
    ├── Azure Production
    ├── GitHub MENTA
    ├── Docker Registry
    ├── MCP Server
    └── SSH
```

A user ezután secretet csatolhat:

```text
User Secret
     ↓
Workspace attachment
     vagy
Project attachment
     ↓
Authorized harness / MCP
```

A secret tehát személyhez tartozik, de használati scope workspace/project lehet.

---

# 24. Secret típusok

Arbitrary secret támogatás:

```text
username/password
API key
Bearer token
SSH key
certificate
client ID + client secret
JSON
arbitrary key/value
```

A Secret Vault ne korlátozódjon MCP loginokra.

---

# 25. Secret usage

Az AI/harness láthassa, hogy milyen credential használható:

```text
azure-prod
type: Azure credential
scope: Project/Azure
status: available
```

A secret értékét azonban lehetőleg ne kelljen az LLM contextjébe adni.

Preferált:

```text
Agent
 ↓
secret_use("azure-prod")
 ↓
credential injection
 ↓
tool / process
```

A credential felhasználható legyen anélkül, hogy az érték a chat/context része lenne.

---

# 26. Secret backend

MVP:

```text
PostgreSQL
  encrypted secret payload
```

Master encryption key:
- environmentből;
- Docker secretből;
- később külső secret managerből.

Architektúra legyen adapteres, hogy később támogatható legyen:

```text
HashiCorp Vault
Azure Key Vault
AWS Secrets Manager
```

---

# 27. Secret audit

Minden secret használat auditálva:

```text
timestamp
user
API key
resource
secret
action
result
source
```

Például:

```text
2026-09-25 10:31
Kristóf
Claude - Company
Project/Azure
azure-prod
USE
SUCCESS
MCP
```

A secret értéke soha ne kerüljön audit logba.

---

# 28. Authentication

Támogatni kell:

```text
Django username/password
OIDC/OAuth2
```

OIDC provider lehet például:
- Microsoft Entra ID;
- Google;
- Keycloak;
- Authentik;
- egyéb kompatibilis OIDC provider.

A user identity legyen független az authentication providertől.

---

# 29. Audit log

Központi audit log minden lényeges műveletről:

```text
user
api_key
timestamp
action
resource
workspace
project
source
IP
user_agent
result
version
git_commit
```

Példák:

```text
READ document
WRITE document
DELETE document
SEARCH
FOLLOW_LINK
CREATE_PROJECT
CHANGE_PERMISSION
CREATE_API_KEY
USE_SECRET
GIT_COMMIT
GIT_PULL
GIT_PUSH
MCP_REQUEST
```

Audit log immutable/append-only szemléletű legyen.

---

# 30. Web UI

Knowledge-centric UI:

```text
Dashboard
├── Workspaces
├── Projects
├── Documents
├── Search
├── Graph / Links
├── Users
├── Groups
├── Permissions
├── API Keys
├── Secrets
├── Git
└── Audit
```

Document view:

```text
┌─────────────────────────────────────────┐
│ Azure Container Apps Deployment        │
│ Company / Azure                         │
│                                         │
│ [Edit] [History] [Permissions]          │
│                                         │
│ Markdown content                        │
│                                         │
│ Related knowledge                       │
│ Backlinks                               │
│ Linked resources                        │
└─────────────────────────────────────────┘
```

A Markdown editor/preview mellett legyen:
- backlinks;
- forward links;
- related knowledge;
- version history;
- diff;
- permissions;
- Git state;
- AI search.

---

# 31. Django architecture

Backend:

```text
Django
Django REST Framework
PostgreSQL
```

Javasolt Django appok:

```text
apps/
├── accounts/
├── workspaces/
├── resources/
├── documents/
├── files/
├── permissions/
├── groups/
├── links/
├── git/
├── search/
├── knowledge/
├── embeddings/
├── mcp/
├── secrets/
├── audit/
└── api/
```

A domain logic ne kerüljön közvetlenül Django view-kba.

Használjunk service layer / application service réteget:

```text
API / MCP
    ↓
Application services
    ↓
Domain / Permission
    ↓
Repositories / adapters
```

---

# 32. Javasolt PostgreSQL modellek

Minimum:

```text
User
Group
GroupMembership

Workspace
Project

Resource
ResourceMembership / ACL

Document
File
DocumentVersion
FileVersion

ResourceLink

GitRepository
GitSyncState
GitCommitReference

ApiKey
ApiKeyScope

Secret
SecretAttachment

KnowledgeChunk
EmbeddingIndexState

AuditEvent
```

A Resource Identity miatt a Resource lehet központi entitás, amelyhez a különböző domain objektumok kapcsolódnak.

---

# 33. Resource ACL

Javasolt modell:

```text
ResourceACL
----------------
resource
subject_type
subject_id
permission
effect
inherit
created_by
created_at
```

subject lehet:

```text
USER
GROUP
API_KEY
```

A permission engine számolja ki az effective permissiont.

---

# 34. API key security

API key:
- csak egyszer mutatott secret;
- hash tárolás;
- prefix azonosításra;
- revoke;
- rotate;
- expiry opcionálisan;
- scope;
- audit.

Például:

```text
ck_live_xxxxxxxxx
```

A DB ne tárolja visszafejthető formában az API key teljes értékét.

---

# 35. Storage architecture

```text
/data/knowledge/
├── workspaces/
│   ├── <workspace-id>/
│   │   ├── documents/
│   │   ├── files/
│   │   └── projects/
│   │       └── <project-id>/
│   │           ├── documents/
│   │           └── files/
│
└── git/
```

A konkrét path legyen absztrakt storage adapteren keresztül kezelve.

Később támogatható:
- local filesystem;
- NFS;
- S3-compatible storage.

---

# 36. Git storage és filesystem

Git-backed resource esetén a repository checkout legyen a resource storage.

Nem Git-backed resource esetén local filesystem.

```text
StorageAdapter
├── LocalFilesystemAdapter
└── GitWorkspaceAdapter
```

A business logic ne tudja, melyiket használja.

---

# 37. Obsidian import

Existing vault:

```text
Obsidian Vault
       ↓
Git repository
       ↓
Attach repository to Workspace/Project
       ↓
Scan
       ↓
Metadata extraction
       ↓
Link extraction
       ↓
Chunking
       ↓
Embedding
```

Az Obsidian linkeket:

```text
[[Some Note]]
```

a rendszer ResourceLink entitásokká alakítsa.

A frontmatter maradjon meg.

---

# 38. MCP + harness

A rendszer legyen használható:

```text
Claude Code
OpenCode
Codex
Hermes
Cursor
egyéb MCP-compatible harness
```

A user a Web UI-ban létrehozhat például:

```text
Claude - Company
```

API/MCP keyt.

A key scope:

```text
Company
  READ/WRITE

Personal
  DENY

HomeLab
  DENY
```

A harness csak ezt a scope-ot kapja.

---

# 39. Example: Company Azure agent

User:

> Deploy this FastAPI application to Azure.

Agent:

```text
1. knowledge_search("Azure FastAPI deployment")
2. permission filtering
3. find approved Azure conventions
4. find Bicep skill
5. find Container App pattern
6. find networking pattern
7. inspect relevant examples
8. generate Bicep
9. request/use Azure credential
10. execute deployment
```

A rendszer így a céges konvenciókat nem csak dokumentációként, hanem aktív AI contextként biztosítja.

---

# 40. Example: cross-workspace knowledge

```text
Company/Azure/deployment.md
          |
          +--> HomeLab/TrueNAS/Overview.md
```

Company agent:

```text
TrueNAS infrastructure exists in HomeLab.
Summary available.
Full document restricted.
```

A Company agent nem követheti tovább a linket teljes tartalomért, ha nincs READ permission.

---

# 41. Security boundary

A legfontosabb security boundary:

```text
External AI
    ↓
MCP/API key
    ↓
Authentication
    ↓
API key scope
    ↓
User permissions
    ↓
Group permissions
    ↓
Resource ACL
    ↓
Resource
```

A semantic search és vector store sem kerülheti meg ezt.

A secret usage külön authorizationt kap.

---

# 42. Docker deployment

A TrueNAS-on Docker Compose alapú deployment.

Javasolt kezdeti komponensek:

```text
knowledge-web
knowledge-worker
postgres
qdrant
```

Opcionális később:

```text
redis
```

Például:

```text
docker-compose.yml

services:
  web:
    build: .
    command: gunicorn ...

  worker:
    build: .
    command: celery ...

  postgres:
    image: postgres:...

  qdrant:
    image: qdrant/qdrant:...
```

A reverse proxy már meglévő **Pangolin** infrastruktúrán keresztül történjen.

A Knowledge Platformnak nem kell saját Nginxet futtatnia.

---

# 43. Background jobs

A következő műveletek ne request threadben fussanak:

```text
Git sync
file scan
metadata extraction
chunking
embedding
re-index
large import
secret scanning
link resolution
```

Worker architecture:

```text
Django
  ↓
job queue
  ↓
worker
  ↓
index/storage
```

Redis/Celery opcionális MVP után, vagy kezdetben egyszerűbb Django management command / worker megoldás.

---

# 44. Secret scanning

A feltöltött/Gitbe commitált fájlokat opcionálisan secret scanner vizsgálja.

Például:
- API key;
- token;
- private key;
- password;
- cloud credential.

Pipeline:

```text
write
 ↓
secret scan
 ↓
found?
 ├── no → continue
 └── yes → warn/reject
```

A Secret Vault legyen az ajánlott credential storage.

A knowledge dokumentumokban ne legyenek valódi secret értékek.

---

# 45. MVP fázisok

## Phase 1 — Core

- Django
- PostgreSQL
- User
- Group
- Workspace
- Project
- Resource Identity
- ACL
- Document/File
- local filesystem
- REST API
- Web UI alap
- Markdown editor
- basic audit

## Phase 2 — Git

- workspace Git repo
- project Git repo
- import existing Obsidian vault
- two-way sync
- commit
- branch
- diff
- optional PR workflow

## Phase 3 — AI Knowledge

- full text search
- chunking
- Qdrant
- embeddings
- semantic search
- reranking
- knowledge priority
- approved/draft workflow

## Phase 4 — MCP

- MCP server
- search/get/list tools
- write tools
- link traversal
- permission enforcement
- API keys
- key scopes
- audit

## Phase 5 — Secret Vault

- user-owned secret store
- encrypted storage
- workspace/project attachment
- secret metadata
- secret use/injection
- secret audit
- secret backend abstraction

## Phase 6 — Enterprise/auth

- OIDC/OAuth
- advanced group management
- permission UI
- API key management
- audit dashboard

## Phase 7 — Advanced AI

- skill discovery
- approved knowledge ranking
- agent-specific knowledge scopes
- knowledge graph
- automatic related knowledge
- AI draft generation
- human approval
- knowledge quality tooling

---

# 46. Non-goals for MVP

Nem cél első verzióban:
- teljes Obsidian klón;
- teljes project management rendszer;
- teljes CI/CD rendszer;
- saját LLM;
- saját Git server;
- saját reverse proxy;
- teljes cloud secret manager kiváltása;
- automatikus, emberi review nélküli céges knowledge approval.

---

# 47. Technológiai javaslat

Backend:

```text
Python
Django
Django REST Framework
PostgreSQL
```

Search:

```text
PostgreSQL full text
Qdrant
embedding provider abstraction
reranker abstraction
```

Storage:

```text
Local filesystem
Git
```

AI interface:

```text
MCP
REST
```

Auth:

```text
Django auth
OIDC/OAuth2
```

Deployment:

```text
Docker Compose
TrueNAS
Pangolin reverse proxy
```

Frontend:

```text
Django templates vagy külön SPA
Markdown editor
```

Az első verzióban nem szükséges külön frontend framework, ha Django-val gyorsabban elérhető egy jól használható UI. A frontend később cserélhető.

---

# 48. Fontos architekturális döntések

1. **Files are source of truth.**
2. PostgreSQL nem tárolja a teljes knowledge contentet authoritative adatként.
3. Git opcionális workspace/project resource.
4. Workspace és project is lehet Git-backed.
5. ACL minden resource típusra közös.
6. MCP és REST ugyanazt a permission engine-t használja.
7. Vector search soha nem bypassolja az ACL-t.
8. Secret Vault user-owned.
9. Secret workspace/project attachmenttel használható.
10. API key scope szűkítheti a user hozzáférését.
11. Secret value nem kerül audit logba.
12. Secret value lehetőleg nem kerül LLM contextbe.
13. Cross-workspace link megengedett, de link traversal permission-checked.
14. Public metadata/summary külön kezelhető a contenttől.
15. AI-generated knowledge alapból draft.
16. Approved knowledge retrievalben preferált.
17. Git workflow resource-onként konfigurálható.
18. OIDC és Django auth egyaránt támogatott.
19. Pangolin kezeli a külső reverse proxy réteget.
20. Minden külső AI access auditálható.

---

# 49. Első fejlesztési sorrend

A coding agent ne egyszerre építse fel az egész rendszert.

Ajánlott sorrend:

```text
01. Django project + Docker
02. PostgreSQL
03. Resource Identity
04. Workspace / Project
05. User / Group
06. Permission engine
07. Files / Documents
08. Markdown UI
09. Audit
10. Git adapter
11. Obsidian import
12. Search
13. Chunking
14. Qdrant
15. MCP
16. API keys
17. Secret Vault
18. OIDC
19. Git branch/PR workflow
20. advanced AI features
```

Minden fázis után legyen működő, tesztelhető rendszer.

---

# 50. Definition of Done — első használható verzió

Az MVP akkor tekinthető működőnek, ha:

1. létrehozható workspace;
2. létrehozható project;
3. mindkettőben lehet fájlokat tárolni;
4. Markdown dokumentum szerkeszthető;
5. user és group kezelhető;
6. workspace/project/document ACL működik;
7. explicit DENY működik;
8. inheritance működik;
9. audit log készül;
10. API key létrehozható;
11. API key scope szűkíthető;
12. REST API permission-aware;
13. MCP permission-aware;
14. meglévő Git repository importálható;
15. Git-backed workspace/project működik;
16. változás verziózódik;
17. diff megjeleníthető;
18. Qdrant index létrehozható;
19. semantic search működik;
20. permission filtering a semantic search után megtörténik;
21. workspace-ek/projectek között linkelés működik;
22. link traversal permission-aware;
23. user saját Secret Vaultot használhat;
24. secret workspace/projecthez csatolható;
25. secret usage auditálható;
26. Pangolin mögött Docker Compose-szal fut.

---

# 51. Példa végállapot

```text
                     KNOWLEDGE PLATFORM
                              │
        ┌─────────────────────┼──────────────────────┐
        │                     │                      │
    Workspaces             Resources              Users
        │                     │                      │
   ┌────┼────┐          ┌─────┼─────┐          ┌─────┴─────┐
   │    │    │          │     │     │          │           │
Personal Company HomeLab Project Document     Users       Groups
             │              │
          Azure           Bicep
             │              │
          Git repo        SKILL.md
                            │
                         Examples
                            │
                            ▼
                    Search / RAG / MCP
                            │
               ┌────────────┼────────────┐
               │            │            │
             Claude       OpenCode      Hermes
               │
               ▼
          Permission Engine
               │
       ┌───────┴────────┐
       │                │
   Knowledge         Secrets
       │                │
       ▼                ▼
    Files/Git      User Secret Vault
```

A rendszer végső célja, hogy a kolléga AI-ja ne „általános AI-ként” működjön, hanem **a cég által kialakított és jóváhagyott mérnöki tudást követő agentként**.

Például:

> „Készítsd el a Bicep deploymentet.”

→ az agent megkeresi a céges Azure/Bicep skillt, approved patternöket, példákat és kapcsolódó döntéseket → permission szerint csak azt használja, amit láthat → szükség esetén a projekthez csatolt credentialt használja → a módosítás auditálódik és verziózódik → opcionális Git branch/PR workflow-n megy keresztül.

Ez képezi a platform elsődleges használati modelljét.
