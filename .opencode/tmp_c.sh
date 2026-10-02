cd '/home/branc/VS projects/Brainbox' || exit 1
git add -A
git status --short
git commit -q -F - <<'MSG'
Web/API/MCP: allow renaming a workspace, through one audited service call

Renaming was only possible with a raw REST PATCH - impossible from the UI and
from MCP - and that PATCH bypassed both the ACL and the audit trail. All three
surfaces now call WorkspaceService.update, which needs ADMIN, writes a
workspace_rename audit entry carrying the previous value, and refuses to touch
the slug (it is in every URL, in the on-disk layout and in the git bookkeeping).

Needed on the UI side because the bootstrap workspace is called "Personal" but
is shared, so after the access-model change there would be two workspaces with
that name and no way to disambiguate in the interface.
MSG
git log --oneline -1 | cat
git push 2>&1 | tail -3