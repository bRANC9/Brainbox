cd '/home/branc/VS projects/Brainbox' || exit 1
git status --short
echo
git add -A
git commit -q -F - <<'MSG'
Web: the deep-folder trail is walkable by clicking, not only by direct URL

The remaining gap from making projects nodes in the tree: a user whose only
grant is a deep folder saw an empty tree on the workspace page. They could
reach the content by search or a pasted URL, but not by clicking from the top
- which makes "private until shared, share a folder" unusable in practice.

Two parts:

* visibility is now computed over **every** folder in the workspace, not only
  the scope-root ones. A readable node deep inside a project lights up its
  ancestors; the rows still *start* only at the scope, so deep folders do not
  leak into the workspace view.
* a structural (unreadable) node links to where the trail continues: a project
  node links to its own page, a folder node to its container. A "way in" that
  cannot be clicked is a dead end wearing a breadcrumb.

Verified in a browser as a user holding only "Fejlesztői rész/runbooks/2024":
the workspace page lists the project as a trail node, clicking it lands on the
project page, and there `runbooks/ -> 2024/ -> Éles runbook` is the readable
chain. Two bugs caught on the way: a project node linked to the *workspace*
(a loop) because the node's container is the workspace, and a nested folder
reported "no access" because the readable set was built only for the rows that
start at this scope. 387 tests, ruff clean.
MSG
git log --oneline -1 | cat
git push 2>&1 | tail -2