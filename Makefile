.PHONY: help pipeline report test app venv flows summary db clean

PYTHON ?= python3

help:
	@echo "make pipeline   rebuild db/consultbae.db from the 3 CSVs"
	@echo "make report     rebuild + export the Task 4 data-issues report"
	@echo "make test       run the test suite"
	@echo "make venv       create .venv and install the audio-app dependencies"
	@echo "make app        run the Task 3 audio app (PORT=5055 by default)"
	@echo "make flows      replay the Task 2 n8n flows against a running app"
	@echo "make summary    show what the last build produced"
	@echo "make db         open the database in the sqlite3 shell"
	@echo "make clean      delete generated artefacts"

pipeline:
	$(PYTHON) -m pipeline.run

report:
	$(PYTHON) -m pipeline.run --report

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

venv:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install --quiet -r requirements.txt
	@echo "done - now run: make app"

# Port 5055 rather than 5000: on macOS, AirPlay Receiver squats on 5000 and
# answers requests before Flask ever sees them.
app: db/consultbae.db
	PORT=$${PORT:-5055} $(if $(wildcard .venv/bin/python),.venv/bin/python,$(PYTHON)) -m app.server

# Checks the API the n8n flows depend on. The flows themselves are the Task 2
# deliverable and live in automation/n8n/ - this is a harness, not a substitute.
flows:
	$(PYTHON) scripts/replay_flows.py

db/consultbae.db:
	$(PYTHON) -m pipeline.run --quiet

summary:
	@sqlite3 -header -column db/consultbae.db \
	  "SELECT run_id, source_rows_read, source_rows_used, people_created, issues_logged FROM load_runs ORDER BY run_id DESC LIMIT 1;"
	@echo ""
	@sqlite3 -header -column db/consultbae.db \
	  "SELECT severity, COUNT(*) AS issues FROM data_issues GROUP BY severity ORDER BY severity;"

db:
	sqlite3 db/consultbae.db

clean:
	rm -f db/consultbae.db
	rm -f reports/*.csv reports/*.json
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
