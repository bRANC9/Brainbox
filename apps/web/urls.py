"""Knowledge-centric web UI routes."""

from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("tree/move/", views.tree_move, name="tree_move"),
    path("tree/folder-op/", views.tree_folder_op, name="tree_folder_op"),
    path("documents/<uuid:pk>/tags/", views.document_tags, name="document_tags"),
    path("folders/tags/", views.folder_tags, name="folder_tags"),
    path("search/", views.search, name="search"),
    path("workspaces/<slug:workspace_slug>/folders/", views.folder_create, name="workspace_folder_create"),
    path(
        "workspaces/<slug:workspace_slug>/<slug:project_slug>/folders/",
        views.folder_create,
        name="project_folder_create",
    ),
    path("discover/", views.discovery, name="discovery"),
    path("calendar/", views.calendar, name="calendar"),
    path("calendar/agenda/", views.agenda, name="agenda"),
    path("calendar/ical/", views.deadlines_ical, name="deadlines_ical"),
    path("workspaces/<slug:workspace_slug>/", views.workspace_detail, name="workspace_detail"),
    # Must stay ABOVE the <project_slug> patterns: a three-segment path under
    # workspaces/ is otherwise swallowed by them and 404s as a missing project.
    path(
        "workspaces/<slug:workspace_slug>/settings/",
        views.workspace_rename,
        name="workspace_rename",
    ),
    path(
        "workspaces/<slug:workspace_slug>/files/",
        views.file_browser,
        name="workspace_files",
    ),
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
        "workspaces/<slug:workspace_slug>/<slug:project_slug>/files/",
        views.file_browser,
        name="project_files",
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
        "workspaces/<slug:workspace_slug>/bulk-upload/",
        views.document_bulk_upload,
        name="workspace_bulk_upload",
    ),
    path(
        "workspaces/<slug:workspace_slug>/<slug:project_slug>/bulk-upload/",
        views.document_bulk_upload,
        name="project_bulk_upload",
    ),
    path("manage/settings/", views.settings_page, name="settings_page"),
    path("manage/settings/test/", views.settings_test, name="settings_test"),
    path("manage/settings/test/", views.settings_test, name="settings_test"),
    path(
        "resources/<uuid:resource_id>/permissions/",
        views.resource_permissions,
        name="resource_permissions",
    ),
    path(
        "resources/<uuid:resource_id>/takeover/",
        views.resource_takeover,
        name="resource_takeover",
    ),
    path("personal/", views.personal_workspace, name="personal_workspace"),
    # Serves the bytes for every file. apps.documents.embeds rewrites relative
    # image/document references to this one route, so it takes a file pk and
    # nothing else.
    path("files/<uuid:pk>/content/", views.file_content, name="file_content"),
    path("documents/<uuid:pk>/", views.document_detail, name="document_detail"),
    path("documents/<uuid:pk>/edit/", views.document_edit, name="document_edit"),
    path("documents/<uuid:pk>/approve/", views.document_approve, name="document_approve"),
    path("documents/<uuid:pk>/reject/", views.document_reject, name="document_reject"),
    path("documents/<uuid:pk>/history/", views.document_history, name="document_history"),
]
