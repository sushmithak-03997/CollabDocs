from django.contrib.auth import authenticate
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min, Prefetch, Q
from rest_framework import mixins, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import AuditLog, Comment, Document, DocumentVersion, Tag, User, Workspace, WorkspaceMember
from .permissions import ADMIN_ONLY, ALL_ROLES, WRITE_ROLES, get_role, require_role
from .serializers import (
    AddMemberSerializer, AuditLogSerializer, CommentSerializer, DocumentSerializer, DocumentVersionSerializer,
    LoginSerializer, TagInputSerializer, TagSerializer, UserSerializer, WorkspaceMemberSerializer,
    WorkspaceSerializer,
)
from .utils import (
    create_version, log_audit, maybe_simulate_failure, parse_datetime_param, parse_uuid_param,
)


def member_workspace_ids(user):
    """values_list: only the IDs are needed, so don't hydrate whole Workspace rows."""
    return WorkspaceMember.objects.filter(user=user).values_list("workspace_id", flat=True)


def apply_date_filters(qs, params, field, prefix_after, prefix_before):
    after, before = params.get(prefix_after), params.get(prefix_before)
    if after:
        qs = qs.filter(**{f"{field}__gte": parse_datetime_param(after, prefix_after)})
    if before:
        qs = qs.filter(**{f"{field}__lte": parse_datetime_param(before, prefix_before, end_of_day=True)})
    return qs


# ================================================================== Users
class UserViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = UserSerializer

    def get_permissions(self):
        if self.action in ("create", "login"):
            return [AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        qs = User.objects.all().order_by("username")
        params = self.request.query_params
        search = params.get("search")
        if search:
            qs = qs.filter(Q(username__icontains=search) | Q(email__icontains=search))
        ids = params.get("ids")
        if ids:
            qs = qs.filter(id__in=[parse_uuid_param(i.strip(), "ids") for i in ids.split(",") if i.strip()])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"user": UserSerializer(user).data, "token": token.key}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="login")
    def login(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(**serializer.validated_data)
        if user is None:
            return Response({"detail": "Invalid username or password."}, status=status.HTTP_401_UNAUTHORIZED)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": UserSerializer(user).data})

    @action(detail=False, methods=["get"], url_path="me")
    def me(self, request):
        return Response(UserSerializer(request.user).data)


# ================================================================== Workspaces
class WorkspaceViewSet(viewsets.ModelViewSet):
    serializer_class = WorkspaceSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            Workspace.objects.filter(id__in=member_workspace_ids(user))
            .select_related("owner")
            .prefetch_related(
                Prefetch("members", queryset=WorkspaceMember.objects.filter(user=user), to_attr="my_memberships")
            )
            .annotate(member_count=Count("members", distinct=True), document_count=Count("documents", distinct=True))
            .order_by("-created_at")
        )
        if self.action != "list":
            return qs
        params = self.request.query_params
        if params.get("search"):
            qs = qs.filter(name__icontains=params["search"])
        if params.get("is_active") is not None:
            qs = qs.filter(is_active=params["is_active"].lower() in ("true", "1", "yes"))
        if params.get("min_members"):
            try:
                qs = qs.filter(member_count__gte=int(params["min_members"]))
            except ValueError:
                raise ValidationError({"min_members": "Must be an integer."})
        if params.get("role"):
            roles = [r.strip() for r in params["role"].split(",")]
            bad = set(roles) - set(WorkspaceMember.Role.values)
            if bad:
                raise ValidationError({"role": f"Invalid role(s): {', '.join(sorted(bad))}."})
            qs = qs.filter(
                id__in=WorkspaceMember.objects.filter(user=user, role__in=roles).values_list("workspace_id", flat=True)
            )
        return apply_date_filters(qs, params, "created_at", "created_after", "created_before")

    def perform_update(self, serializer):
        require_role(self.request.user, serializer.instance.pk, ADMIN_ONLY)
        with transaction.atomic():
            workspace = serializer.save()
            log_audit(
                self.request.user, AuditLog.Action.UPDATED, "Workspace", workspace.pk,
                workspace=workspace, details={"name": workspace.name, "is_active": workspace.is_active},
            )

    def perform_destroy(self, instance):
        """Soft delete: the workspace is deactivated, its data is kept."""
        require_role(self.request.user, instance.pk, ADMIN_ONLY)
        with transaction.atomic():
            instance.is_active = False
            instance.save(update_fields=["is_active", "updated_at"])
            log_audit(
                self.request.user, AuditLog.Action.DELETED, "Workspace", instance.pk,
                workspace=instance, details={"soft_delete": True},
            )

    @action(detail=True, methods=["get", "post"], url_path="members")
    def members(self, request, pk=None):
        workspace = self.get_object()
        if request.method == "GET":
            qs = WorkspaceMember.objects.filter(workspace=workspace).select_related("user", "workspace")
            role = request.query_params.get("role")
            if role:
                qs = qs.filter(role__in=[r.strip() for r in role.split(",")])
            return Response(WorkspaceMemberSerializer(qs.order_by("joined_at"), many=True).data)

        require_role(request.user, workspace.pk, ADMIN_ONLY)
        serializer = AddMemberSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = User.objects.get(pk=serializer.validated_data["user_id"])
        except User.DoesNotExist:
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        try:
            with transaction.atomic():
                member = WorkspaceMember.objects.create(
                    workspace=workspace, user=user, role=serializer.validated_data["role"]
                )
                log_audit(
                    request.user, AuditLog.Action.MEMBER_ADDED, "WorkspaceMember", member.pk,
                    workspace=workspace, details={"user": user.username, "role": member.role},
                )
        except IntegrityError:
            return Response(
                {"detail": f"User '{user.username}' is already a member of this workspace."},
                status=status.HTTP_409_CONFLICT,
            )
        member = WorkspaceMember.objects.select_related("user", "workspace").get(pk=member.pk)
        return Response(WorkspaceMemberSerializer(member).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, pk=None):
        workspace = self.get_object()
        docs = Document.objects.filter(workspace=workspace)
        Status = Document.Status
        doc_stats = docs.aggregate(
            total=Count("id"),
            drafts=Count("id", filter=Q(status=Status.DRAFT)),
            published=Count("id", filter=Q(status=Status.PUBLISHED)),
            archived=Count("id", filter=Q(status=Status.ARCHIVED)),
        )
        members_by_role = {
            row["role"]: row["count"]
            for row in WorkspaceMember.objects.filter(workspace=workspace).values("role").annotate(count=Count("id"))
        }
        comment_stats = Comment.objects.filter(document__workspace=workspace).aggregate(
            total=Count("id"), unique_commenters=Count("author", distinct=True)
        )
        version_total = DocumentVersion.objects.filter(document__workspace=workspace).count()
        top_contributors = [
            {"username": row["created_by__username"], "documents": row["documents"]}
            for row in docs.values("created_by__username")
            .annotate(documents=Count("id"))
            .order_by("-documents", "created_by__username")[:5]
        ]
        return Response(
            {
                "workspace": workspace.name,
                "documents": doc_stats,
                "members_by_role": members_by_role,
                "comments": comment_stats,
                "total_versions": version_total,
                "top_contributors": top_contributors,
            }
        )


# ================================================================== Documents
class DocumentViewSet(viewsets.ModelViewSet):
    serializer_class = DocumentSerializer
    ORDERING_FIELDS = {"created_at", "updated_at", "title"}

    def get_queryset(self):
        user = self.request.user
        qs = (
            Document.objects.filter(workspace_id__in=member_workspace_ids(user))
            .select_related("workspace", "created_by")
            .prefetch_related("tags")
            .annotate(version_count=Count("versions", distinct=True))
        )
        if self.action != "list":
            return qs

        params = self.request.query_params
        if params.get("workspace"):
            qs = qs.filter(workspace_id=parse_uuid_param(params["workspace"], "workspace"))
        if params.get("status"):
            statuses = [s.strip() for s in params["status"].split(",") if s.strip()]
            bad = set(statuses) - set(Document.Status.values)
            if bad:
                raise ValidationError({"status": f"Invalid status value(s): {', '.join(sorted(bad))}."})
            qs = qs.filter(status__in=statuses)
        if params.get("created_by"):
            qs = qs.filter(created_by_id=parse_uuid_param(params["created_by"], "created_by"))
        if params.get("tag"):
            qs = qs.filter(tags__name__in=[t.strip().lower() for t in params["tag"].split(",") if t.strip()])
        if params.get("search"):  # Q objects: OR across several columns
            term = params["search"]
            qs = qs.filter(
                Q(title__icontains=term) | Q(content__icontains=term) | Q(tags__name__icontains=term)
            )
        qs = apply_date_filters(qs, params, "created_at", "created_after", "created_before")
        qs = apply_date_filters(qs, params, "updated_at", "updated_after", "updated_before")

        ordering = params.get("ordering", "-updated_at")
        if ordering.lstrip("-") not in self.ORDERING_FIELDS:
            raise ValidationError({"ordering": f"Use one of: {', '.join(sorted(self.ORDERING_FIELDS))} (prefix - for desc)."})
        return qs.order_by(ordering).distinct()

    # --- create / update: document save + version snapshot + audit row in ONE atomic block
    def perform_create(self, serializer):
        user = self.request.user
        require_role(user, serializer.validated_data["workspace"].pk, WRITE_ROLES)
        with transaction.atomic():
            document = serializer.save(created_by=user)  # post_save signal writes the AuditLog row here
            maybe_simulate_failure(self.request, "document saved + audit row written, version not yet created")
            create_version(document, user)

    def perform_update(self, serializer):
        user = self.request.user
        require_role(user, serializer.instance.workspace_id, WRITE_ROLES)
        with transaction.atomic():
            serializer.instance._actor = user  # signal records who made the edit
            document = serializer.save()
            maybe_simulate_failure(self.request, "document updated + audit row written, version not yet created")
            create_version(document, user)

    def perform_destroy(self, instance):
        require_role(self.request.user, instance.workspace_id, ADMIN_ONLY)
        with transaction.atomic():
            log_audit(
                self.request.user, AuditLog.Action.DELETED, "Document", instance.pk,
                workspace=instance.workspace, details={"title": instance.title},
            )
            instance.delete()

    @action(detail=True, methods=["get"], url_path="versions")
    def versions(self, request, pk=None):
        document = self.get_object()
        qs = DocumentVersion.objects.filter(document=document).select_related("edited_by", "document")
        params = request.query_params
        try:
            if params.get("version_gte"):
                qs = qs.filter(version_number__gte=int(params["version_gte"]))
            if params.get("version_lte"):
                qs = qs.filter(version_number__lte=int(params["version_lte"]))
        except ValueError:
            raise ValidationError({"detail": "version_gte / version_lte must be integers."})
        if params.get("edited_by"):
            qs = qs.filter(edited_by_id=parse_uuid_param(params["edited_by"], "edited_by"))
        qs = qs.order_by("-version_number")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(DocumentVersionSerializer(page, many=True).data)

    @action(detail=True, methods=["get"], url_path="summary")
    def summary(self, request, pk=None):
        document = self.get_object()
        comment_stats = Comment.objects.filter(document=document).aggregate(
            total=Count("id"),
            top_level=Count("id", filter=Q(parent__isnull=True)),
            replies=Count("id", filter=Q(parent__isnull=False)),
            unique_commenters=Count("author", distinct=True),
        )
        version_qs = DocumentVersion.objects.filter(document=document)
        version_stats = version_qs.aggregate(
            total=Count("id"), contributors=Count("edited_by", distinct=True),
            first_saved=Min("created_at"), last_saved=Max("created_at"),
        )
        latest = version_qs.select_related("edited_by").order_by("-version_number").first()
        return Response(
            {
                "id": document.pk,
                "title": document.title,
                "status": document.status,
                "workspace": document.workspace.name,
                "tags": list(document.tags.order_by("name").values_list("name", flat=True)),
                "versions": version_stats,
                "latest_version": {
                    "version_number": latest.version_number,
                    "edited_by": str(latest.edited_by) if latest else None,
                } if latest else None,
                "comments": comment_stats,
            }
        )

    @action(detail=True, methods=["get", "post"], url_path="tags")
    def tags(self, request, pk=None):
        document = self.get_object()
        if request.method == "GET":
            return Response(list(document.tags.order_by("name").values_list("name", flat=True)))

        require_role(request.user, document.workspace_id, WRITE_ROLES)
        serializer = TagInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        names = sorted({t.strip().lower() for t in serializer.validated_data["tags"] if t.strip()})
        if not names:
            raise ValidationError({"tags": "Provide at least one non-blank tag name."})
        with transaction.atomic():
            tag_objs = [Tag.objects.get_or_create(name=n)[0] for n in names]
            document.tags.add(*tag_objs)  # M2M join table written automatically
            log_audit(
                request.user, AuditLog.Action.UPDATED, "Document", document.pk,
                workspace=document.workspace, details={"tags_added": names},
            )
        return Response(
            {"document": document.pk, "tags": list(document.tags.order_by("name").values_list("name", flat=True))},
            status=status.HTTP_201_CREATED,
        )


# ================================================================== Comments
class CommentViewSet(viewsets.ModelViewSet):
    serializer_class = CommentSerializer

    def get_queryset(self):
        qs = (
            Comment.objects.filter(document__workspace_id__in=member_workspace_ids(self.request.user))
            .select_related("author", "document", "parent")
            .annotate(reply_count=Count("replies", distinct=True))
        )
        if self.action != "list":
            return qs
        params = self.request.query_params
        if params.get("document"):
            qs = qs.filter(document_id=parse_uuid_param(params["document"], "document"))
        if params.get("parent"):
            qs = qs.filter(parent_id=parse_uuid_param(params["parent"], "parent"))
        if params.get("top_level", "").lower() in ("true", "1"):
            qs = qs.filter(parent__isnull=True)
        if params.get("author"):
            qs = qs.filter(author_id=parse_uuid_param(params["author"], "author"))
        if params.get("search"):
            qs = qs.filter(body__icontains=params["search"])
        return apply_date_filters(qs, params, "created_at", "created_after", "created_before").order_by("created_at")

    def _require_author_or_admin(self, comment):
        user = self.request.user
        if comment.author_id != user.pk and get_role(user, comment.document.workspace_id) != "admin":
            raise PermissionDenied("Only the comment's author or a workspace admin can modify this comment.")

    def perform_create(self, serializer):
        user = self.request.user
        document = serializer.validated_data["document"]
        require_role(user, document.workspace_id, ALL_ROLES)  # viewers may comment
        with transaction.atomic():
            comment = serializer.save(author=user)
            log_audit(
                user, AuditLog.Action.CREATED, "Comment", comment.pk,
                workspace=document.workspace, details={"document": str(document.pk)},
            )

    def perform_update(self, serializer):
        self._require_author_or_admin(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._require_author_or_admin(instance)
        instance.delete()


# ================================================================== Tags
class TagViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = TagSerializer

    def get_queryset(self):
        # filter() BEFORE annotate(): the Count only covers documents in the user's workspaces
        qs = (
            Tag.objects.filter(documents__workspace_id__in=member_workspace_ids(self.request.user))
            .annotate(document_count=Count("documents", distinct=True))
            .order_by("-document_count", "name")
        )
        params = self.request.query_params
        if params.get("search"):
            qs = qs.filter(name__icontains=params["search"])
        if params.get("names"):
            qs = qs.filter(name__in=[n.strip().lower() for n in params["names"].split(",") if n.strip()])
        if params.get("min_documents"):
            try:
                qs = qs.filter(document_count__gte=int(params["min_documents"]))
            except ValueError:
                raise ValidationError({"min_documents": "Must be an integer."})
        return qs


# ================================================================== Audit logs
class AuditLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only. Visible to admins of the workspace the entry belongs to."""

    serializer_class = AuditLogSerializer

    def get_queryset(self):
        admin_workspaces = WorkspaceMember.objects.filter(
            user=self.request.user, role=WorkspaceMember.Role.ADMIN
        ).values_list("workspace_id", flat=True)
        qs = AuditLog.objects.filter(workspace_id__in=admin_workspaces).select_related("actor", "workspace")
        if self.action != "list":
            return qs
        params = self.request.query_params
        if params.get("model_name"):
            qs = qs.filter(model_name__iexact=params["model_name"])
        if params.get("action"):
            actions = [a.strip() for a in params["action"].split(",")]
            bad = set(actions) - set(AuditLog.Action.values)
            if bad:
                raise ValidationError({"action": f"Invalid action(s): {', '.join(sorted(bad))}."})
            qs = qs.filter(action__in=actions)
        if params.get("actor"):
            qs = qs.filter(actor_id=parse_uuid_param(params["actor"], "actor"))
        if params.get("object_id"):
            qs = qs.filter(object_id=parse_uuid_param(params["object_id"], "object_id"))
        if params.get("workspace"):
            qs = qs.filter(workspace_id=parse_uuid_param(params["workspace"], "workspace"))
        return apply_date_filters(qs, params, "timestamp", "from", "to")
