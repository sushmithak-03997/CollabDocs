from django.test import override_settings
from rest_framework.test import APITestCase

from .models import AuditLog, Comment, Document, DocumentVersion, Workspace, WorkspaceMember

PASSWORD = "StrongPass123!"


class BaseAPITest(APITestCase):
    def register(self, username):
        r = self.client.post(
            "/api/users/",
            {"username": username, "email": f"{username}@example.com", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.data["user"]["id"], r.data["token"]

    def auth(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

    def setUp(self):
        self.alice_id, self.alice_token = self.register("alice")
        self.bob_id, self.bob_token = self.register("bob")
        self.auth(self.alice_token)
        r = self.client.post("/api/workspaces/", {"name": "Engineering", "description": "Eng docs"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.ws_id = r.data["id"]

    def make_doc(self, **extra):
        payload = {"workspace": self.ws_id, "title": "Design Doc", "content": "v1 content", **extra}
        r = self.client.post("/api/documents/", payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.data["id"]


class UserAndAuthTests(BaseAPITest):
    def test_duplicate_email_rejected(self):
        r = self.client.post(
            "/api/users/", {"username": "other", "email": "ALICE@example.com", "password": PASSWORD}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("email", r.data)

    def test_login_and_unauthenticated(self):
        self.client.credentials()
        self.assertEqual(self.client.get("/api/workspaces/").status_code, 401)
        r = self.client.post("/api/users/login/", {"username": "alice", "password": PASSWORD}, format="json")
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/users/login/", {"username": "alice", "password": "nope"}, format="json")
        self.assertEqual(r.status_code, 401)


class WorkspaceTests(BaseAPITest):
    def test_creation_adds_owner_as_admin_and_audit(self):
        m = WorkspaceMember.objects.get(workspace_id=self.ws_id, user_id=self.alice_id)
        self.assertEqual(m.role, WorkspaceMember.Role.ADMIN)
        self.assertTrue(AuditLog.objects.filter(model_name="Workspace", object_id=self.ws_id, action="created").exists())

    @override_settings(DEBUG=True)
    def test_workspace_creation_rolls_back_on_failure(self):
        before = (Workspace.objects.count(), WorkspaceMember.objects.count(), AuditLog.objects.count())
        r = self.client.post(
            "/api/workspaces/", {"name": "Doomed"}, format="json", HTTP_X_SIMULATE_FAILURE="true"
        )
        self.assertEqual(r.status_code, 500)
        after = (Workspace.objects.count(), WorkspaceMember.objects.count(), AuditLog.objects.count())
        self.assertEqual(before, after)

    def test_duplicate_member_returns_409(self):
        body = {"user_id": self.bob_id, "role": "editor"}
        self.assertEqual(self.client.post(f"/api/workspaces/{self.ws_id}/members/", body, format="json").status_code, 201)
        r = self.client.post(f"/api/workspaces/{self.ws_id}/members/", body, format="json")
        self.assertEqual(r.status_code, 409)

    def test_add_unknown_user_404_and_non_admin_403(self):
        r = self.client.post(
            f"/api/workspaces/{self.ws_id}/members/",
            {"user_id": "00000000-0000-4000-8000-000000000000"}, format="json",
        )
        self.assertEqual(r.status_code, 404)
        self.client.post(f"/api/workspaces/{self.ws_id}/members/", {"user_id": self.bob_id, "role": "viewer"}, format="json")
        self.auth(self.bob_token)
        r = self.client.post(f"/api/workspaces/{self.ws_id}/members/", {"user_id": self.alice_id}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_non_member_cannot_see_workspace(self):
        self.auth(self.bob_token)
        self.assertEqual(self.client.get(f"/api/workspaces/{self.ws_id}/").status_code, 404)
        self.assertEqual(self.client.get("/api/workspaces/").data["count"], 0)

    def test_list_filters_and_stats(self):
        self.make_doc()
        r = self.client.get("/api/workspaces/?search=engi&min_members=1&is_active=true")
        self.assertEqual(r.data["count"], 1)
        self.assertEqual(r.data["results"][0]["document_count"], 1)
        self.assertEqual(r.data["results"][0]["my_role"], "admin")
        stats = self.client.get(f"/api/workspaces/{self.ws_id}/stats/").data
        self.assertEqual(stats["documents"]["total"], 1)
        self.assertEqual(stats["documents"]["drafts"], 1)
        self.assertEqual(stats["members_by_role"], {"admin": 1})
        self.assertEqual(self.client.get("/api/workspaces/?created_after=garbage").status_code, 400)


class DocumentTests(BaseAPITest):
    def test_create_makes_version_1_and_audit_via_signal(self):
        doc_id = self.make_doc()
        self.assertEqual(DocumentVersion.objects.get(document_id=doc_id).version_number, 1)
        log = AuditLog.objects.get(model_name="Document", object_id=doc_id)
        self.assertEqual(log.action, "created")
        self.assertEqual(str(log.actor_id), self.alice_id)

    def test_update_makes_version_2_and_updated_audit(self):
        doc_id = self.make_doc()
        r = self.client.put(
            f"/api/documents/{doc_id}/",
            {"workspace": self.ws_id, "title": "Design Doc", "content": "v2 content", "status": "published"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.data["version_count"], 2)
        self.assertEqual(list(DocumentVersion.objects.filter(document_id=doc_id).values_list("version_number", flat=True).order_by("version_number")), [1, 2])
        self.assertEqual(AuditLog.objects.filter(object_id=doc_id, model_name="Document", action="updated").count(), 1)
        versions = self.client.get(f"/api/documents/{doc_id}/versions/?version_gte=2").data
        self.assertEqual(versions["count"], 1)
        self.assertEqual(versions["results"][0]["content"], "v2 content")

    @override_settings(DEBUG=True)
    def test_document_create_rolls_back_document_version_and_audit(self):
        r = self.client.post(
            "/api/documents/", {"workspace": self.ws_id, "title": "Doomed doc"},
            format="json", HTTP_X_SIMULATE_FAILURE="true",
        )
        self.assertEqual(r.status_code, 500)
        self.assertFalse(Document.objects.filter(title="Doomed doc").exists())
        self.assertEqual(DocumentVersion.objects.count(), 0)
        self.assertFalse(AuditLog.objects.filter(model_name="Document").exists())

    @override_settings(DEBUG=True)
    def test_document_update_rolls_back(self):
        doc_id = self.make_doc()
        r = self.client.patch(f"/api/documents/{doc_id}/", {"content": "changed"}, format="json", HTTP_X_SIMULATE_FAILURE="true")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(Document.objects.get(pk=doc_id).content, "v1 content")
        self.assertEqual(DocumentVersion.objects.filter(document_id=doc_id).count(), 1)
        self.assertFalse(AuditLog.objects.filter(object_id=doc_id, action="updated").exists())

    def test_viewer_cannot_write_but_can_read(self):
        doc_id = self.make_doc()
        self.client.post(f"/api/workspaces/{self.ws_id}/members/", {"user_id": self.bob_id, "role": "viewer"}, format="json")
        self.auth(self.bob_token)
        self.assertEqual(self.client.get(f"/api/documents/{doc_id}/").status_code, 200)
        r = self.client.patch(f"/api/documents/{doc_id}/", {"content": "hack"}, format="json")
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/documents/", {"workspace": self.ws_id, "title": "Nope nope"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_validation(self):
        r = self.client.post("/api/documents/", {"workspace": self.ws_id, "title": "ab"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("title", r.data)
        doc_id = self.make_doc(status="archived")
        r = self.client.patch(f"/api/documents/{doc_id}/", {"content": "edit"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/documents/not-a-uuid/")
        self.assertEqual(r.status_code, 404)

    def test_q_search_filters_tags_and_summary(self):
        a = self.make_doc(title="Python guide", content="django rest")
        self.make_doc(title="Cooking", content="pasta", status="published")
        self.client.post(f"/api/documents/{a}/tags/", {"tags": ["Python", "backend"]}, format="json")
        self.assertEqual(self.client.get("/api/documents/?search=python").data["count"], 1)   # title OR tag match, once
        self.assertEqual(self.client.get("/api/documents/?search=pasta").data["count"], 1)
        self.assertEqual(self.client.get("/api/documents/?tag=backend").data["count"], 1)
        self.assertEqual(self.client.get("/api/documents/?status=draft,published").data["count"], 2)
        self.assertEqual(self.client.get("/api/documents/?status=bogus").status_code, 400)
        self.assertEqual(self.client.get("/api/documents/?created_after=2000-01-01&created_before=2100-01-01").data["count"], 2)
        tags = self.client.get("/api/tags/?min_documents=1").data["results"]
        self.assertEqual({t["name"]: t["document_count"] for t in tags}, {"python": 1, "backend": 1})
        s = self.client.get(f"/api/documents/{a}/summary/").data
        self.assertEqual(s["versions"]["total"], 1)
        self.assertEqual(s["tags"], ["backend", "python"])


class CommentAndAuditTests(BaseAPITest):
    def test_threading_and_validation(self):
        doc1, doc2 = self.make_doc(), self.make_doc(title="Second doc")
        top = self.client.post("/api/comments/", {"document": doc1, "body": "Top level"}, format="json")
        self.assertEqual(top.status_code, 201, top.content)
        reply = self.client.post("/api/comments/", {"document": doc1, "body": "Reply", "parent": top.data["id"]}, format="json")
        self.assertEqual(reply.status_code, 201)
        wrong = self.client.post("/api/comments/", {"document": doc2, "body": "x", "parent": top.data["id"]}, format="json")
        self.assertEqual(wrong.status_code, 400)
        blank = self.client.post("/api/comments/", {"document": doc1, "body": "   "}, format="json")
        self.assertEqual(blank.status_code, 400)
        listing = self.client.get(f"/api/comments/?document={doc1}&top_level=true").data
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["results"][0]["reply_count"], 1)
        # deleting the parent keeps the reply (SET_NULL)
        self.client.delete(f"/api/comments/{top.data['id']}/")
        self.assertIsNone(Comment.objects.get(pk=reply.data["id"]).parent)

    def test_only_author_or_admin_can_edit_comment(self):
        doc = self.make_doc()
        self.client.post(f"/api/workspaces/{self.ws_id}/members/", {"user_id": self.bob_id, "role": "viewer"}, format="json")
        self.auth(self.bob_token)
        c = self.client.post("/api/comments/", {"document": doc, "body": "bob says hi"}, format="json").data
        self.assertEqual(self.client.patch(f"/api/comments/{c['id']}/", {"body": "edited"}, format="json").status_code, 200)
        self.auth(self.alice_token)  # admin can moderate
        self.assertEqual(self.client.delete(f"/api/comments/{c['id']}/").status_code, 204)

    def test_audit_log_endpoint_scope_and_filters(self):
        doc = self.make_doc()
        self.client.patch(f"/api/documents/{doc}/", {"content": "edit"}, format="json")
        r = self.client.get(f"/api/audit-logs/?model_name=document&action=created,updated&object_id={doc}")
        self.assertEqual(r.data["count"], 2)
        self.assertEqual({row["action"] for row in r.data["results"]}, {"created", "updated"})
        self.auth(self.bob_token)  # not an admin of anything
        self.assertEqual(self.client.get("/api/audit-logs/").data["count"], 0)
