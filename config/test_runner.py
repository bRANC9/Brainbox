"""Keep password hashing inexpensive only while running the test suite."""

from django.test import override_settings
from django.test.runner import DiscoverRunner


class FastPasswordTestRunner(DiscoverRunner):
    def run_tests(self, test_labels, **kwargs):
        with override_settings(
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
        ):
            return super().run_tests(test_labels, **kwargs)
