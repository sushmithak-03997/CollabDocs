# CollabDocs API

Backend API for a collaborative document platform (simplified Notion / Google Docs): workspaces, role-based
membership, versioned documents, threaded comments, tags and an audit trail. Built with Django + Django REST
Framework + PostgreSQL. API only — use Postman as the client.

## Tech
Python 3.12+, Django, Django REST Framework (token auth), PostgreSQL, python-dotenv. Dependencies are pinned in `requirements.txt`.

## Setup

```bash
# 1. clone and enter the repo
git clone <repository-url>
cd collabdocs

# 2. virtual environment + dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. start PostgreSQL (Docker)
docker run --name collabdocs-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=collabdocs -p 5433:5432 -d postgres:16

# 4. environment variables
cp .env.example .env               # then set DB_PASSWORD=postgres, DB_HOST=127.0.0.1, DB_PORT=5433

# 5. apply migrations (all migrations are committed and apply cleanly from scratch)
python manage.py migrate

# 6. run the server
python manage.py runserver         # http://127.0.0.1:8000/api/
```

Wait a few seconds after step 3 before running migrations so PostgreSQL can finish starting.
Using a local PostgreSQL instead of Docker? Create a database named `collabdocs` and put its credentials in `.env`.

Run the tests: `python manage.py test`
(`DB_ENGINE=sqlite python manage.py test` runs them on SQLite for a quick local check; the project targets PostgreSQL.)

## Authentication
Token auth. Register (`POST /api/users/`) returns a token; send it as `Authorization: Token <token>`.
The Postman collection does this automatically through the `{{token}}` collection variable.

## Postman
Import `CollabDocs.postman_collection.json` (repo root). Run folders top-to-bottom
(Users → Workspaces → Documents → Comments → Tags → Audit Logs); test scripts save `token`, `workspace_id`,
`document_id`, etc. into collection variables. Requests prefixed **DEMO** trigger the rollback demonstration.

## Endpoints

| # | Method & path | Purpose |
|---|---|---|
| 1 | `POST /api/users/` | Register (returns token) |
| 2 | `GET /api/users/{id}/` | Get a user (also `GET /users/`, `/users/me/`, `POST /users/login/`) |
| 3 | `POST /api/workspaces/` | Create workspace; owner added as admin in one atomic block |
| 4 | `GET /api/workspaces/` | List my workspaces. Filters: `search`, `is_active`, `min_members`, `role`, `created_after`, `created_before` |
| 5 | `GET/PUT/PATCH/DELETE /api/workspaces/{id}/` | Detail / update (admin) / soft-delete (admin, sets `is_active=false`) |
| 6 | `POST /api/workspaces/{id}/members/` | Add member (admin). Duplicate → **409** (also `GET` to list members) |
| 7 | `GET /api/workspaces/{id}/stats/` | Aggregations: docs by status, members by role, comments, top contributors |
| 8 | `POST /api/documents/` | Create document + version 1 + audit log, atomically |
| 9 | `GET /api/documents/` | List. `search` (Q: title OR content OR tag), `status` (in), `tag`, `workspace`, `created_by`, `created_after/before`, `updated_after/before`, `ordering` |
| 10 | `GET /api/documents/{id}/` | Detail |
| 11 | `PUT/PATCH /api/documents/{id}/` | Update; creates a new version atomically; signal writes `updated` audit entry |
| 12 | `GET /api/documents/{id}/versions/` | Version history (`version_gte`, `version_lte`, `edited_by`) |
| 13 | `GET /api/documents/{id}/summary/` | Aggregations: version + comment statistics |
| 14 | `POST /api/comments/` | Comment / threaded reply (`parent`) |
| 15 | `GET /api/comments/` | List. `document`, `parent`, `top_level`, `author`, `search`, `created_after/before` |
| 16 | `POST /api/documents/{id}/tags/` | Add tags (`{"tags": ["python"]}`), creates tags as needed |
| 17 | `GET /api/tags/` | Tags with document counts (`search`, `names`, `min_documents`) |
| 18 | `GET /api/audit-logs/` | Audit trail (workspace admins). `model_name`, `action`, `actor`, `object_id`, `workspace`, `from`, `to` |

Roles: **admin** (everything, manage members, delete), **editor** (create/edit documents, tags), **viewer** (read + comment).
Non-members get **404** for workspace-scoped resources (existence is not leaked); wrong role gets **403**.
Status codes used: 200, 201, 204, 400, 401, 403, 404, 409.

## Design notes
- **UUID primary keys** on all 8 models (abstract `UUIDModel`, `uuid.uuid4`, `editable=False`). Enums use `TextChoices`.
- **Transactions**: workspace + admin member + audit row; document save + `DocumentVersion` + audit row; member add + audit row — each in one `transaction.atomic()`.
- **Signal** (`core/signals.py`, connected in `CoreConfig.ready()`): `post_save` on `Document` writes an `AuditLog` (actor, action, model_name, object_id). Because Django sets `instance._state.adding = False` *before* `post_save` fires, a `pre_save` receiver captures `_state.adding` so created vs. updated is detected correctly. The signal runs inside the view's atomic block, so the audit row rolls back with the document.
- **Middleware** (`core/middleware.py`): prints `[METHOD] /path -> status (x.xx ms)` for every request.
- **Query optimisation**: `select_related` on user/workspace/document joins, `prefetch_related` for tags, `values_list` for ID subqueries, `Q` for OR search, `Count`/`aggregate`/`annotate` in workspace list, workspace stats, document list, document summary and tag list. N+1 is avoided by reading annotations in `SerializerMethodField`s.
- **Error handling**: `DoesNotExist` → 404, `IntegrityError` → 409 (explicit in add-member, plus a global handler in `core/exceptions.py`).

## Demonstrating transaction rollback
With `DEBUG=True`, send the header `X-Simulate-Failure: true` to *Create workspace*, *Create document* or *Update document*
(the requests prefixed **DEMO** in the Postman collection). The request fails with a 500 after the first writes but before the
transaction completes; afterwards the list endpoints and audit logs show that nothing was persisted. The header is ignored when `DEBUG=False`.

## Demo video
Walkthrough covering an atomic transaction with rollback, the request-logging middleware, an aggregation endpoint, and the audit log written by the signal after a document update:

**[Watch the demo part 1](https://www.loom.com/edit/329ab5f90976483c929e0320f7bf0993)**
**[Watch the demo part 2](https://www.loom.com/share/ab64aebcfc844f8d90f7284f62019b46)**
