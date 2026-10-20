from rest_framework.exceptions import PermissionDenied

from .models import WorkspaceMember

Role = WorkspaceMember.Role
ALL_ROLES = {Role.ADMIN, Role.EDITOR, Role.VIEWER}
WRITE_ROLES = {Role.ADMIN, Role.EDITOR}
ADMIN_ONLY = {Role.ADMIN}


def get_role(user, workspace_id):
    return (
        WorkspaceMember.objects.filter(workspace_id=workspace_id, user=user).values_list("role", flat=True).first()
    )


def require_role(user, workspace_id, allowed=ALL_ROLES):
    """Return the user's role in the workspace or raise 403 with a clear message."""
    role = get_role(user, workspace_id)
    if role is None:
        raise PermissionDenied("You are not a member of this workspace.")
    if role not in allowed:
        needed = " or ".join(sorted(r.value for r in allowed))
        raise PermissionDenied(f"Your role '{role}' cannot perform this action (requires: {needed}).")
    return role
