from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("workspaces", views.WorkspaceViewSet, basename="workspace")
router.register("documents", views.DocumentViewSet, basename="document")
router.register("comments", views.CommentViewSet, basename="comment")
router.register("tags", views.TagViewSet, basename="tag")
router.register("audit-logs", views.AuditLogViewSet, basename="auditlog")

urlpatterns = router.urls
