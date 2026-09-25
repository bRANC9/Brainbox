"""Knowledge-centric web UI routes."""

from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("search/", views.search, name="search"),
    path("discover/", views.discovery, name="discovery"),
    path("workspaces/<slug:workspace_slug>/", views.workspace_detail, name="workspace_detail"),
    path(
        "workspaces/<slug:workspace_slug>/documents/new/",
        views.document_create,
        name="workspace_document_create",
    ),
    path(
        "workspaces/<slug:workspace_slug>/git/pull/",
        views.workspace_git_pull,
        name="workspace_git_pull",
    ),
    path(
        "workspaces/<slug:workspace_slug>/<slug:project_slug>/",
        views.project_detail,
        name="project_detail",
    ),
    path(
        "workspaces/<slug:workspace_slug>/<slug:project_slug>/documents/new/",
        views.document_create,
        name="project_document_create",
    ),
    path(
        "workspaces/<slug:workspace_slug>/<slug:project_slug>/git/pull/",
        views.project_git_pull,
        name="project_git_pull",
    ),
    path("manage/api-keys/", views.api_keys, name="api_keys"),
    path("manage/audit/", views.audit_dashboard, name="audit_dashboard"),
    path("manage/groups/", views.groups_admin, name="groups_admin"),
    path(
        "resources/<uuid:resource_id>/permissions/",
        views.resource_permissions,
        name="resource_permissions",
    ),
    path("documents/<uuid:pk>/", views.document_detail, name="document_detail"),
    path("documents/<uuid:pk>/edit/", views.document_edit, name="document_edit"),
    path("documents/<uuid:pk>/approve/", views.document_approve, name="document_approve"),
    path("documents/<uuid:pk>/reject/", views.document_reject, name="document_reject"),
    path("documents/<uuid:pk>/history/", views.document_history, name="document_history"),
]
