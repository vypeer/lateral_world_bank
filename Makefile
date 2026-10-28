.PHONY: install run app test lint typecheck check fixtures clean

install:  ## create .venv from the lockfile
	uv sync --frozen

run: install  ## run the full pipeline from a clean checkout
	uv run wbpipe run

app: run  ## live dashboard on http://localhost:8501 (builds the warehouse first)
	uv run --extra app streamlit run src/wbpipe/app.py

test:
	uv sync --frozen --extra app
	uv run --extra app pytest

lint: install
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv sync --frozen --extra app
	uv run --extra app mypy

check: lint typecheck test  ## everything CI runs

fixtures:  ## re-capture live API fixtures (calls the real API; never run by tests)
	python3 scripts/capture_fixtures.py

clean:
	rm -rf data/ .venv .pytest_cache .mypy_cache .ruff_cache
