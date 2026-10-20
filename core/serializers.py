from django.contrib.auth import password_validation
from django.db import transaction
from rest_framework import serializers

from .models import AuditLog, Comment, Document, DocumentVersion, Tag, User, Workspace, WorkspaceMember
from .permissions import get_role
from .utils import log_audit, maybe_simulate_failure


# ---------------------------------------------------------------- Users
class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "password", "date_joined"]
        read_only_fields = ["id", "date_joined"]

    # custom validation #1
    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


# ---------------------------------------------------------------- Workspaces
class WorkspaceSerializer(serializers.ModelSerializer):
    owner = serializers.StringRelatedField(read_only=True)
    member_count = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()
    my_role = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = [
            "id", "name", "description", "owner", "is_active",
            "member_count", "document_count", "my_role", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    # SerializerMethodField #1-3 (read annotations from the queryset to avoid N+1; fall back to a query)
    def get_member_count(self, obj):
        value = getattr(obj, "member_count", None)
        return value if value is not None else obj.members.count()

    def get_document_count(self, obj):
        value = getattr(obj, "document_count", None)
        return value if value is not None else obj.documents.count()

    def get_my_role(self, obj):
        memberships = getattr(obj, "my_memberships", None)
        if memberships is not None:
            return memberships[0].role if memberships else None
        return get_role(self.context["request"].user, obj.pk)

    # custom validation #2
    def validate_name(self, value):
        value = value.strip()
        if len(value) < 3:
            raise serializers.ValidationError("Workspace name must be at least 3 characters long.")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        # workspace + admin membership + audit entry: all-or-nothing
        with transaction.atomic():
            workspace = Workspace.objects.create(owner=request.user, **validated_data)
            maybe_simulate_failure(request, "workspace row created, owner not yet added as admin")
            WorkspaceMember.objects.create(workspace=workspace, user=request.user, role=WorkspaceMember.Role.ADMIN)
            log_audit(
                request.user, AuditLog.Action.CREATED, "Workspace", workspace.pk,
                workspace=workspace, details={"name": workspace.name},
            )
        return workspace


class WorkspaceMemberSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = WorkspaceMember
        fields = ["id", "workspace", "user", "username", "email", "role", "joined_at"]
        read_only_fields = fields


class AddMemberSerializer(serializers.Serializer):
    user_id = serializers.UUIDField()
    role = serializers.ChoiceField(choices=WorkspaceMember.Role.choices, default=WorkspaceMember.Role.VIEWER)


# ---------------------------------------------------------------- Documents
class DocumentSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    created_by_id = serializers.UUIDField(read_only=True)
    tags = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")
    version_count = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id", "workspace", "title", "content", "status",
            "created_by", "created_by_id", "tags", "version_count", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_version_count(self, obj):
        value = getattr(obj, "version_count", None)
        return value if value is not None else obj.versions.count()

    def validate_title(self, value):
        value = value.strip()
        if len(value) < 3:
            raise serializers.ValidationError("Title must be at least 3 characters long.")
        return value

    def validate_workspace(self, value):
        if self.instance is not None and value != self.instance.workspace:
            raise serializers.ValidationError("A document cannot be moved to another workspace.")
        if not value.is_active:
            raise serializers.ValidationError("This workspace is inactive; documents cannot be created or edited.")
        return value

    def validate(self, attrs):
        # custom validation: archived documents are read-only until un-archived
        if self.instance is not None and self.instance.status == Document.Status.ARCHIVED:
            still_archived = attrs.get("status", Document.Status.ARCHIVED) == Document.Status.ARCHIVED
            edits_content = any(k in attrs and attrs[k] != getattr(self.instance, k) for k in ("title", "content"))
            if still_archived and edits_content:
                raise serializers.ValidationError(
                    "Archived documents are read-only. Set status to 'draft' or 'published' to edit."
                )
        return attrs


class DocumentVersionSerializer(serializers.ModelSerializer):
    edited_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = DocumentVersion
        fields = ["id", "document", "version_number", "title", "content", "edited_by", "created_at"]
        read_only_fields = fields


# ---------------------------------------------------------------- Comments
class CommentSerializer(serializers.ModelSerializer):
    author = serializers.StringRelatedField(read_only=True)
    reply_count = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ["id", "document", "author", "parent", "body", "reply_count", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_reply_count(self, obj):
        value = getattr(obj, "reply_count", None)
        return value if value is not None else obj.replies.count()

    def validate_body(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Comment body cannot be blank.")
        return value

    def validate(self, attrs):
        instance = self.instance
        if instance is not None and "document" in attrs and attrs["document"] != instance.document:
            raise serializers.ValidationError({"document": "A comment cannot be moved to another document."})
        document = attrs.get("document", instance.document if instance else None)
        parent = attrs.get("parent", instance.parent if instance else None)
        if parent is not None:
            if document is not None and parent.document_id != document.pk:
                raise serializers.ValidationError({"parent": "Parent comment belongs to a different document."})
            if instance is not None and parent.pk == instance.pk:
                raise serializers.ValidationError({"parent": "A comment cannot be its own parent."})
        return attrs


# ---------------------------------------------------------------- Tags & Audit
class TagSerializer(serializers.ModelSerializer):
    document_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tag
        fields = ["id", "name", "document_count", "created_at"]
        read_only_fields = fields


class TagInputSerializer(serializers.Serializer):
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50), allow_empty=False, max_length=20
    )


class AuditLogSerializer(serializers.ModelSerializer):
    actor = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = AuditLog
        fields = ["id", "actor", "action", "model_name", "object_id", "workspace", "details", "timestamp"]
        read_only_fields = fields
