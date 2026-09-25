"""REST API v1 routes."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("workspaces", views.WorkspaceViewSet, basename="workspace")
router.register("projects", views.ProjectViewSet, basename="project")
router.register("resources", views.ResourceViewSet, basename="resource")
router.register("documents", views.DocumentViewSet, basename="document")
router.register("files", views.FileViewSet, basename="file")
router.register("links", views.ResourceLinkViewSet, basename="link")
router.register("git", views.GitRepositoryViewSet, basename="git")
router.register("users", views.UserViewSet, basename="user")
router.register("groups", views.GroupViewSet, basename="group")
router.register("permissions", views.ResourceACLViewSet, basename="permission")
router.register("api-keys", views.ApiKeyViewSet, basename="api-key")
router.register("secrets", views.SecretViewSet, basename="secret")
router.register("audit", views.AuditEventViewSet, basename="audit")

urlpatterns = router.urls + [
    path("search/", views.SearchView.as_view(), name="search"),
    path("discovery/", views.DiscoveryView.as_view(), name="discovery"),
    path("quality/", views.QualityView.as_view(), name="quality"),
    path("drafts/", views.DraftCreateView.as_view(), name="drafts"),
]
