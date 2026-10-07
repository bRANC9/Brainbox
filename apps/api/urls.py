"""REST API v1 routes."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.jobs import api as jobs_views

from . import views


class ApiRouter(DefaultRouter):
    """DefaultRouter without its API root view.

    `/api/v1/` used to answer with DRF's browsable index: a second surface that
    only repeated the collection names, without schemas, permissions or a single
    instruction an agent could follow. `/llm` is that surface now.
    """

    include_root_view = False


router = ApiRouter()
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
router.register("gateway", views.GatewayTargetViewSet, basename="gateway")
router.register("curator", views.CuratorProposalViewSet, basename="curator")
router.register("comments", views.CommentViewSet, basename="comment")
router.register("webhooks", views.WebhookViewSet, basename="webhook")
router.register("saved-searches", views.SavedSearchViewSet, basename="saved-search")
router.register("audit", views.AuditEventViewSet, basename="audit")
router.register("jobs", jobs_views.JobViewSet, basename="job")
router.register("job-runs", jobs_views.JobRunViewSet, basename="job-run")
router.register("deadlines", views.DeadlineViewSet, basename="deadline")
router.register("folders", views.FolderViewSet, basename="folder")
router.register("settings", views.RuntimeSettingViewSet, basename="setting")

urlpatterns = router.urls + [
    path("llm/", views.LLMGuideView.as_view(), name="llm"),
    path("search/", views.SearchView.as_view(), name="search"),
    path("discovery/", views.DiscoveryView.as_view(), name="discovery"),
    path("memory/", views.MemoryView.as_view(), name="memory"),
    path("quality/", views.QualityView.as_view(), name="quality"),
    path("drafts/", views.DraftCreateView.as_view(), name="drafts"),
]
