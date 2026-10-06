"""Knowledge-centric web UI routes.

Two shapes, and only two:

* the workspace root: ``workspaces/<slug>/<action>/`` - what lives directly in a
  workspace, which is not a node in the tree.
* any node: ``workspaces/<slug>/f/<tree_path>/<action>/`` - a project, a folder,
  or a folder several levels inside one. A project is a node, so it uses the same
  shape as a folder; there is no project-slug form left.

Route order matters inside the ``f/`` group: ``<path:tree_path>`` is greedy, so
the action patterns have to be tried before the bare node page, or
``f/Deploy/documents/new/`` would match the node route with the tree path
"Deploy/documents/new" and 404.
"""

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
    path("discover/", views.discovery, name="discovery"),
    path("calendar/", views.calendar, name="calendar"),
    path("calendar/agenda/", views.agenda, name="agenda"),
    path("calendar/ical/", views.deadlines_ical, name="deadlines_ical"),
    # -- workspace root: content that sits directly in a workspace -------------
    path("workspaces/<slug:workspace_slug>/", views.workspace_detail, name="workspace_detail"),
    path(
        "workspaces/<slug:workspace_slug>/settings/",
        views.workspace_rename,
        name="workspace_rename",
    ),
    path(
        "workspaces/<slug:workspace_slug>/folders/",
        views.folder_create,
        name="workspace_folder_create",
    ),
    path(
        "workspaces/<slug:workspace_slug>/documents/new/",
        views.document_create,
        name="workspace_document_create",
    ),
    path(
        "workspaces/<slug:workspace_slug>/bulk-upload/",
        views.document_bulk_upload,
        name="workspace_bulk_upload",
    ),
    path(
        "workspaces/<slug:workspace_slug>/files/",
        views.file_browser,
        name="workspace_files",
    ),
    path(
        "workspaces/<slug:workspace_slug>/git/pull/",
        views.git_pull,
        name="workspace_git_pull",
    ),
    # -- any node, by its workspace-relative path -----------------------------
    path(
        "workspaces/<slug:workspace_slug>/f/<path:tree_path>/documents/new/",
        views.document_create,
        name="node_document_create",
    ),
    path(
        "workspaces/<slug:workspace_slug>/f/<path:tree_path>/folders/",
        views.folder_create,
        name="node_folder_create",
    ),
    path(
        "workspaces/<slug:workspace_slug>/f/<path:tree_path>/bulk-upload/",
        views.document_bulk_upload,
        name="node_bulk_upload",
    ),
    path(
        "workspaces/<slug:workspace_slug>/f/<path:tree_path>/files/",
        views.file_browser,
        name="node_files",
    ),
    path(
        "workspaces/<slug:workspace_slug>/f/<path:tree_path>/git/pull/",
        views.git_pull,
        name="node_git_pull",
    ),
    path(
        "workspaces/<slug:workspace_slug>/f/<path:tree_path>/",
        views.folder_detail,
        name="folder_detail",
    ),
    path("manage/api-keys/", views.api_keys, name="api_keys"),
    path("gateway/", views.gateway, name="gateway"),
    path("gateway/<uuid:pk>/audit/", views.gateway_audit, name="gateway_audit"),
    path("curator/", views.curator, name="curator"),
    path("manage/audit/", views.audit_dashboard, name="audit_dashboard"),
    path("manage/groups/", views.groups_admin, name="groups_admin"),
    path("manage/settings/", views.settings_page, name="settings_page"),
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
