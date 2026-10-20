# Common tasks. Everything runs in Docker; see README.md.
AREAS ?= 32
BUILD ?= dev
CORES ?= 4
export UID := $(shell id -u)
export GID := $(shell id -g)
RUN = docker compose run --rm pipeline

.PHONY: help image build app eval test areas shell clean

help:
	@echo "make build [AREAS=32] [BUILD=dev]  download sources and build tiles for community areas"
	@echo "make app                           run the viewer at http://localhost:5173"
	@echo "make eval                          screenshot the eval viewpoints (app must be running)"
	@echo "make test                          run pipeline and app checks"
	@echo "make areas                         list the 77 community areas"

image:
	docker compose build pipeline

build: image
	$(RUN) snakemake -s pipeline/Snakefile -d . --cores $(CORES) --config areas=$(AREAS) build=$(BUILD)

app:
	docker compose up app

eval:
	docker compose run --rm eval sh -c "npm ci --no-audit --no-fund && node render.mjs"

test: image
	$(RUN) pytest -q pipeline/tests
	docker compose run --rm --no-deps app sh -c "corepack pnpm install --frozen-lockfile && corepack pnpm check"

areas: image
	$(RUN) python -m cim areas

shell: image
	$(RUN) bash

clean:
	rm -rf data/work data/builds
