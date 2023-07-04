.PHONY: test

all: format check

format: prettier isort black

check: mypy pyright pylint flake8 checkisort checkblack checkprettier

prettier:
	poetry run ./node_modules/.bin/prettier --write lnbits

pyright:
	poetry run ./node_modules/.bin/pyright

black:
	poetry run black .

flake8:
	poetry run flake8

mypy:
	poetry run mypy

isort:
	poetry run isort .

pylint:
	poetry run pylint *.py lnbits/ tools/ tests/

checkprettier:
	poetry run ./node_modules/.bin/prettier --check lnbits

checkblack:
	poetry run black --check .

checkisort:
	poetry run isort --check-only .

test:
	LNBITS_BACKEND_WALLET_CLASS="FakeWallet" \
	FAKE_WALLET_SECRET="ToTheMoon1" \
	LNBITS_DATA_FOLDER="./tests/data" \
	PYTHONUNBUFFERED=1 \
	DEBUG=true \
	poetry run pytest

test-real-wallet:
	LNBITS_DATA_FOLDER="./tests/data" \
	PYTHONUNBUFFERED=1 \
	DEBUG=true \
	poetry run pytest

test-migration:
	rm -rf ./migration-data
	mkdir -p ./migration-data
	unzip tests/data/mock_data.zip -d ./migration-data
	HOST=0.0.0.0 \
	PORT=5002 \
	LNBITS_DATA_FOLDER="./migration-data" \
	timeout 5s poetry run lnbits --host 0.0.0.0 --port 5002 || code=$?; if [[ $code -ne 124 && $code -ne 0 ]]; then exit $code; fi
	HOST=0.0.0.0 \
	PORT=5002 \
	LNBITS_DATABASE_URL="postgres://lnbits:lnbits@localhost:5432/migration" \
	timeout 5s poetry run lnbits --host 0.0.0.0 --port 5002 || code=$?; if [[ $code -ne 124 && $code -ne 0 ]]; then exit $code; fi
	LNBITS_DATA_FOLDER="./migration-data" \
	LNBITS_DATABASE_URL="postgres://lnbits:lnbits@localhost:5432/migration" \
	poetry run python tools/conv.py

migration:
	poetry run python tools/conv.py

openapi:
	LNBITS_BACKEND_WALLET_CLASS="FakeWallet" \
	FAKE_WALLET_SECRET="ToTheMoon1" \
	LNBITS_DATA_FOLDER="./tests/data" \
	PYTHONUNBUFFERED=1 \
	HOST=0.0.0.0 \
	PORT=5003 \
	poetry run lnbits &
	sleep 7
	curl -s http://0.0.0.0:5003/openapi.json | poetry run openapi-spec-validator --errors=all -
	# kill -9 %1

bak:
	# LNBITS_DATABASE_URL=postgres://postgres:postgres@0.0.0.0:5432/postgres
	#

sass:
	npm run sass

bundle:
	npm install
	npm run sass
	npm run vendor_copy
	npm run vendor_json
	poetry run ./node_modules/.bin/prettier -w ./lnbits/static/vendor.json
	npm run vendor_bundle_css
	npm run vendor_minify_css
	npm run vendor_bundle_js
	npm run vendor_minify_js
	# increment serviceworker version
	sed -i -e "s/CACHE_VERSION =.*/CACHE_VERSION = $$(awk '/CACHE_VERSION =/ { print 1+$$4 }' lnbits/core/static/js/service-worker.js)/" \
		lnbits/core/static/js/service-worker.js

install-pre-commit-hook:
	@echo "Installing pre-commit hook to git"
	@echo "Uninstall the hook with poetry run pre-commit uninstall"
	poetry run pre-commit install

pre-commit:
	poetry run pre-commit run --all-files

create-clients:
	LNBITS_EXTENSIONS_DEFAULT_INSTALL="" \
	LNBITS_TITLE="lnbits client" \
	LNBITS_BACKEND_WALLET_CLASS="FakeWallet" \
	LNBITS_DATA_FOLDER="./tests/data" \
	PYTHONUNBUFFERED=1 \
	HOST=0.0.0.0 \
	PORT=5004 \
	poetry run lnbits &
	sleep 7
	rm -rf clients
	mkdir -p clients/browser
	mkdir -p clients/node
	# mkdir -p clients/java
	# mkdir -p clients/python
	# mkdir -p clients/rust
	curl -s http://0.0.0.0:5004/openapi.json > clients/openapi.json
	npx openapi-generator-cli generate -i clients/openapi.json -g typescript-fetch -o clients/browser --additional-properties=npmName=@lnbits/client-browser,supportsES6=true,withInterfaces=true
	npx openapi-generator-cli generate -i clients/openapi.json -g typescript-node -o clients/node --additional-properties=npmName=@lnbits/client,supportsES6=true,withInterfaces=true
	# npx openapi-generator-cli generate -i clients/openapi.json -g java -o clients/java
	# npx openapi-generator-cli generate -i clients/openapi.json -g python -o clients/python
	# npx openapi-generator-cli generate -i clients/openapi.json -g rust -o clients/rust
	killall python

sync-clients:
	sh tools/sync_clients.sh
