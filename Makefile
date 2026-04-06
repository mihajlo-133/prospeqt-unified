.PHONY: dev test screenshots qa

PORT ?= 8000

dev:
	uvicorn app.main:app --reload --port $(PORT)

test:
	python -m pytest tests/ -v

screenshots:
	python qa/screenshot.py --port $(PORT) --prefix manual --wait

qa:
	uvicorn app.main:app --port $(PORT) & echo $$! > .server.pid
	sleep 3 && python qa/screenshot.py --port $(PORT) --prefix qa --wait
	kill $$(cat .server.pid) 2>/dev/null; rm -f .server.pid
	python -m pytest tests/ -v
	@echo "QA complete. Check qa/screenshots/"
