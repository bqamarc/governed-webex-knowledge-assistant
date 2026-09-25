.PHONY: install format lint test verify public-check publication-check build migrate api worker

PYTHON ?= python3

install:
	$(PYTHON) -m pip install -e '.[dev]'

format:
	$(PYTHON) -m ruff format src tests scripts migrations
	$(PYTHON) -m ruff check --fix src tests scripts migrations

lint:
	$(PYTHON) -m ruff check src tests scripts migrations
	$(PYTHON) -m ruff format --check src tests scripts migrations

test:
	$(PYTHON) -m pytest

public-check:
	$(PYTHON) scripts/verify_public_release.py .

publication-check:
	$(PYTHON) scripts/verify_public_release.py --require-license .

build:
	$(PYTHON) -m build

verify: lint test build publication-check

migrate:
	$(PYTHON) -m alembic upgrade head

api:
	$(PYTHON) -m webex_knowledge_assistant.cli api

worker:
	$(PYTHON) -m webex_knowledge_assistant.cli worker
