"""Knowledge-centric web UI routes."""

from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
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
    path("documents/<uuid:pk>/", views.document_detail, name="document_detail"),
    path("documents/<uuid:pk>/edit/", views.document_edit, name="document_edit"),
    path("documents/<uuid:pk>/history/", views.document_history, name="document_history"),
]
