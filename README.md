# CollabDocs API

CollabDocs is a backend service for collaborative document management. It provides workspaces with
role-based membership, documents with a complete version history, threaded comments, tags and an audit
trail of changes. The service is built with Django REST Framework, runs on SQLite by default and can be
switched to PostgreSQL through configuration. It exposes an API only; Postman is used as the client.

## Table of Contents

1. [Key Features](#key-features)
2. [Technology](#technology)
3. [Getting Started](#getting-started)
4. [Authentication](#authentication)
5. [Roles and Permissions](#roles-and-permissions)
6. [API Reference](#api-reference)
7. [Data Integrity and Error Handling](#data-integrity-and-error-handling)
8. [Postman Collection](#postman-collection)
9. [Demo Walkthrough](#demo-walkthrough)

---

## Demo
---

## Key Features

- **Workspaces and membership.** Each workspace has members with one of three roles: admin, editor or viewer.
- **Versioned documents.** Every create or update saves a new document version as part of the same transaction.
- **Threaded comments.** Replies are linked to a parent comment within the same document.
- **Tags.** Tags are created on demand and attached to documents.
- **Audit logging.** Document changes and key workspace actions are recorded in an audit log.
- **Request logging.** Middleware prints the method, path, status code and processing time of each request.

---

## Technology

| Component | Details |
|---|---|
| Language | Python 3.11 or later |
| Framework | Django and Django REST Framework |
| Database | SQLite (default), PostgreSQL (optional) |
| Client | Postman (collection included in the repository) |

---

## Getting Started

### 1. Install dependencies

Create a virtual environment and install the required packages:

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure the environment

Copy the example environment file and adjust the values for your machine:

```bash
cp .env.example .env              # Windows: copy .env.example .env
```

| Variable | Description |
|---|---|
| `SECRET_KEY` | Django secret key. Use a unique value. |
| `DEBUG` | Enables Django debug mode for local development. |
| `ALLOWED_HOSTS` | Comma-separated list of permitted hostnames. |
| `DB_ENGINE` | Set to `postgresql` to use PostgreSQL. Leave unset to use SQLite. |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | PostgreSQL connection settings. Not needed for SQLite. |

The `.env` file is excluded from version control and must never be committed.

### 3. Use PostgreSQL

SQLite requires no setup. To work against PostgreSQL instead, create a role and a database, then set
`DB_ENGINE=postgresql` and the matching `DB_*` values in `.env`:

```bash
psql -U postgres -c "CREATE USER collabdocs WITH PASSWORD 'your-password';"
psql -U postgres -c "CREATE DATABASE collabdocs OWNER collabdocs;"
```

### 4. Apply migrations

```bash
python manage.py migrate
```

### 5. Start the server

```bash
python manage.py runserver
```

The API is available at `http://127.0.0.1:8000/api/`. Keep this terminal open while testing: the
request-logging middleware writes one line per request, showing the HTTP method, the path, the response
status and the elapsed time in milliseconds.

---

## Authentication

The API uses HTTP Basic authentication. Registration (`POST /api/users/`) is open to everyone; every other
endpoint requires a valid username and password. In Postman, open the Authorization tab and choose Basic
Auth, or set the `username` and `password` collection variables.

---

## Roles and Permissions

| Role | Permissions |
|---|---|
| Admin | Full control of the workspace, including adding members. |
| Editor | Create and update documents, add tags and post comments. |
| Viewer | Read documents and post comments. |

Users can only see workspaces they belong to, along with the documents and comments inside them.

---

## API Reference

All routes are served under `/api/`. The collection contains 17 requests, listed below by Postman folder.

| Folder | Method | Path | Description |
|---|---|---|---|
| Users | POST | `/api/users/` | Register a new user. |
| Users | GET | `/api/users/` | List users. Filters: `username`, `email`, `ids`. |
| Workspaces | POST | `/api/workspaces/` | Create a workspace. The workspace, its first admin member and an audit entry are saved atomically. |
| Workspaces | GET | `/api/workspaces/` | List workspaces. Filters: `name`, `is_active`, `ids`, `created_after`, `created_before`. |
| Workspaces | GET | `/api/workspaces/{id}/` | Retrieve a single workspace. |
| Workspaces | POST | `/api/workspaces/{id}/members/` | Add a member. Returns 409 if the user is already a member. |
| Workspaces | GET | `/api/workspaces/{id}/stats/` | Aggregated counts, such as documents by status and members by role. |
| Documents | POST | `/api/documents/` | Create a document. The document, its first version and an audit entry are saved atomically. |
| Documents | GET | `/api/documents/` | List documents. Filters: `q` (title or content), `status__in`, `tag`, `workspace`, `title` and date ranges. |
| Documents | GET | `/api/documents/{id}/` | Retrieve a single document. |
| Documents | PUT | `/api/documents/{id}/` | Update a document. A new version is stored atomically and a signal records the audit entry. |
| Documents | GET | `/api/documents/{id}/versions/` | List the version history of a document. |
| Comments | POST | `/api/comments/` | Add a comment. Include `parent` to reply to an existing comment. |
| Comments | GET | `/api/comments/` | List comments. Filters: `document`, `author`, `q`, `top_level` and date ranges. |
| Tags | POST | `/api/documents/{id}/tags/` | Attach a tag to a document, creating the tag if it does not exist. |
| Tags | GET | `/api/tags/` | List tags. Filters: `name`, `name__in`. |
| Audit Logs | GET | `/api/audit-logs/` | List audit entries. Filters: `model_name`, `action__in`, `object_id__in` and date ranges. |

The standard viewset routes (retrieve, PATCH and DELETE, where enabled) are also available.

---

## Data Integrity and Error Handling

**Status codes**

| Code | Meaning |
|---|---|
| 400 | Validation error in the request. |
| 401 / 403 | Missing or invalid credentials, or insufficient role. |
| 404 | Resource not found. |
| 409 | Conflict, such as a duplicate workspace member or tag. |

**Transactions.** Operations that touch several tables run inside a single database transaction. If any
step fails, every change made by that request is rolled back, including the audit entry.

**Tags on document creation.** `POST /api/documents/` also accepts an optional, write-only `tag_names` list
of existing tags. If any name does not exist, the request returns 404 and the document, its first version
and its audit entry are all rolled back. This behavior is used in the demo to show a failed request leaving
no partial data behind.

---

## Postman Collection

Import `CollabDocs.postman_collection.json` from the repository root into Postman and run the requests in
order, from top to bottom. The test scripts save the identifiers they create into collection variables, so
later requests can reuse them without manual copying.

Before running the List users request, register a second user by editing the body of the Register request.
This provides a member id for the Add member request.

---

## Demo Walkthrough

The project walkthrough is recorded in two parts. Together they cover the API through the Postman
collection and explain how the main backend workflows behave.

Registration and authentication, workspace creation and membership, document creation and versioning, comments, and tags. 
 [Watch Part 1](https://www.loom.com/edit/329ab5f90976483c929e0320f7bf0993) 
 [Watch Part 2](https://www.loom.com/share/ab64aebcfc844f8d90f7284f62019b46) 

The outline below mirrors the two recordings and can be followed with the Postman collection.

### Core API walkthrough

1. **Users.** Register a user and confirm that registration needs no credentials. Register a second user,
   then list users to obtain its id.
2. **Workspaces.** Create a workspace and note that the creator becomes its admin member. List workspaces
   and apply a few filters.
3. **Membership.** Add the second user to the workspace, then repeat the request to show the 409 response
   for a duplicate member.
4. **Documents.** Create a document and confirm that version 1 exists. Update it with PUT and review the
   version history. Try the `q` and `status__in` filters on the list endpoint.
5. **Comments.** Post a top-level comment, then a reply using `parent`. List comments with
   `top_level=true`.
6. **Tags.** Attach tags to the document, then list all tags.

### Backend behavior and verification

1. **Request logging.** With the server console visible, show the log line printed for each request,
   including status and timing.
2. **Aggregation.** Call the workspace stats endpoint and review the counts by document status and member
   role.
3. **Audit trail.** List audit logs filtered by `model_name` and `action__in`. Show the entries created by
   the document create and update requests from Part 1.
4. **Transactional rollback.** Send `POST /api/documents/` with a `tag_names` value that does not exist and
   confirm the 404 response. Then verify that no document or version was saved and that no new audit entry
   appears, which shows the transaction was rolled back as a whole.
