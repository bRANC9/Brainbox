from pathlib import Path

from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOTS = [REPO_ROOT / "templates", *sorted(REPO_ROOT.glob("apps/*/templates"))]


class TemplateCompilationTests(SimpleTestCase):
    """Every shipped template must compile.

    Django allows a ``{% block %}`` name only once per template, even when the
    two definitions sit in opposite branches of an ``{% if %}``. A duplicate
    therefore fails at *parse* time, which turns any view rendering that
    template into a 500 -- regardless of the request. Compiling all templates
    in one test turns that class of bug into a single red test.
    """

    def _all_templates(self):
        seen = set()
        for root in TEMPLATE_ROOTS:
            if not root.is_dir():
                continue
            for path in root.rglob("*.html"):
                name = path.relative_to(root).as_posix()
                if name not in seen:
                    seen.add(name)
                    yield name

    def test_every_template_compiles(self):
        failures = {}
        for name in sorted(self._all_templates()):
            try:
                get_template(name)
            except TemplateDoesNotExist:
                continue
            except Exception as exc:  # TemplateSyntaxError and friends
                failures[name] = f"{type(exc).__name__}: {exc}"
        self.assertEqual(
            failures,
            {},
            "templates failed to compile:\n"
            + "\n".join(f"  {name}\n      {msg}" for name, msg in failures.items()),
        )

    def test_document_form_title_block_is_unique(self):
        """The regression itself: one {% block title %}, not two."""
        source = (TEMPLATE_ROOTS[0] / "document_form.html").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("{% block title %}"),
            1,
            "document_form.html must declare {% block title %} exactly once",
        )
