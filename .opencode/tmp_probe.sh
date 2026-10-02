cd '/home/branc/VS projects/Brainbox' || exit 1
echo "=== does the local dev DB hold the same instance the MCP tools see? ==="
./.venv-local/bin/python manage.py shell -c "
from apps.workspaces.models import Workspace, Project
from apps.documents.models import Document
print('workspaces:')
for w in Workspace.objects.all():
    print('  ', w.pk, repr(w.name), 'slug=', w.slug, 'kind=', getattr(w, 'kind', 'n/a'), 'created_by=', w.created_by_id)
print('projects:')
for p in Project.objects.all():
    print('  ', p.pk, repr(p.name), 'ws=', p.workspace_id)
print('documents:', Document.objects.count())
" 2>&1 | tail -20