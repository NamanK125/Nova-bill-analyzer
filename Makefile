.PHONY: help setup samples api demo run test lint clean

help:
	@echo "Nova trade-doc — local commands"
	@echo ""
	@echo "  ./run.sh         one-shot: boot the app, open UI, then run tests"
	@echo ""
	@echo "  make setup       install python deps + init sqlite"
	@echo "  make samples     synthesise ACME BoL PDFs (clean / mismatch / uncertain)"
	@echo "  make api         run FastAPI on :8080 (UI served at /)"
	@echo "  make demo        headless CLI run on samples/acme_bol_mismatch.pdf"
	@echo "  make test        pytest"
	@echo "  make lint        ruff"
	@echo "  make clean       wipe ./data + caches"

setup:
	mkdir -p data data/artifacts
	cp -n .env.example .env || true
	pip install -e ".[dev]"
	python -c "from nova.store.models import init_sync; init_sync()"

samples:
	mkdir -p samples
	python -m nova.pdf.synth_bol --out samples/acme_bol_clean.pdf     --variant clean
	python -m nova.pdf.synth_bol --out samples/acme_bol_mismatch.pdf  --variant mismatch
	python -m nova.pdf.synth_bol --out samples/acme_bol_uncertain.pdf --variant uncertain

api:
	uvicorn nova.api.main:app --host 0.0.0.0 --port 8080 --reload

demo:
	python -m nova run samples/acme_bol_mismatch.pdf --customer acme

test:
	pytest -v

lint:
	ruff check src/ tests/

clean:
	rm -rf data/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
