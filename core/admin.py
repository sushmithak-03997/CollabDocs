from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AuditLog, Comment, Document, DocumentVersion, Tag, User, Workspace, WorkspaceMember

admin.site.register(User, UserAdmin)
for model in (Workspace, WorkspaceMember, Document, DocumentVersion, Comment, Tag, AuditLog):
    admin.site.register(model)
