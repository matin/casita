.PHONY: install check docs demo clean

install:
	uv sync

check:
	uv run python -m compileall src scripts
	uv run pytest
	uv run python scripts/validate_public.py
	uv run zensical build --clean
	uv build
	uv run casita --help >/dev/null

docs:
	uv run zensical serve

demo:
	uv run casita demo

clean:
	rm -rf .cache dist site tmp
