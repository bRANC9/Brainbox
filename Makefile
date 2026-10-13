.PHONY: check ci lint djlint django-check migrations test

COMPOSE := docker compose -f docker-compose.dev.yml
TEST_RUN := $(COMPOSE) run --rm tests

check: ci

ci: lint djlint django-check migrations test

lint:
	ruff check .

djlint:
	djlint templates --lint --profile=django --ignore H021,H023,H025,D018

django-check:
	$(TEST_RUN) sh -c 'python manage.py check --deploy || python manage.py check'

migrations:
	$(TEST_RUN) python manage.py makemigrations --check --dry-run

test:
	$(TEST_RUN) python manage.py test -v 2
