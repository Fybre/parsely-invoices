# ---------------------------------------------------------------------------
# Invoice Processing Pipeline — Makefile
# Usage: make <target>
# ---------------------------------------------------------------------------

IMAGE   := invoice-pipeline:latest
COMPOSE := docker compose
RUN     := $(COMPOSE) run --rm pipeline

# Colour helpers
BOLD  := \033[1m
RESET := \033[0m
GREEN := \033[32m
CYAN  := \033[36m

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

.PHONY: build
build:                        ## Build the Docker image
	$(COMPOSE) build

.PHONY: rebuild
rebuild:                      ## Force a clean rebuild (no cache)
	$(COMPOSE) build --no-cache

.PHONY: pull
pull:                         ## Pull the latest base images
	docker pull python:3.12-slim

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

.PHONY: up
up:                           ## Start pipeline (watch mode) + dashboard as background services
	WATCH_MODE=true $(COMPOSE) up -d pipeline dashboard
	@echo "$(CYAN)Pipeline running in watch mode.$(RESET)"
	@echo "$(CYAN)Dashboard: http://localhost:$${DASHBOARD_PORT:-8080}$(RESET)"
	@echo "$(CYAN)Logs: make logs   Stop: make down$(RESET)"

.PHONY: dashboard
dashboard:                    ## Start only the dashboard (without starting the pipeline)
	$(COMPOSE) up -d dashboard
	@echo "$(CYAN)Dashboard: http://localhost:$${DASHBOARD_PORT:-8080}$(RESET)"

.PHONY: down
down:                         ## Stop all background services
	$(COMPOSE) down

.PHONY: logs
logs:                         ## Follow logs from all running services
	$(COMPOSE) logs -f pipeline dashboard

.PHONY: logs-pipeline
logs-pipeline:                ## Follow pipeline logs only
	$(COMPOSE) logs -f pipeline

.PHONY: logs-dashboard
logs-dashboard:               ## Follow dashboard logs only
	$(COMPOSE) logs -f dashboard

.PHONY: check
check:                        ## Verify Ollama + data files are ready
	$(RUN) check

.PHONY: process
process:                      ## Process all PDFs in invoices/ (batch mode)
	$(RUN) process /app/invoices

.PHONY: process-verbose
process-verbose:              ## Batch process with debug logging
	$(RUN) -v process /app/invoices

.PHONY: watch
watch:                        ## Watch invoices/ and process new PDFs continuously (Ctrl-C to stop)
	$(COMPOSE) run --rm pipeline watch /app/invoices

.PHONY: watch-verbose
watch-verbose:                ## Watch mode with debug logging
	$(COMPOSE) run --rm pipeline -v watch /app/invoices

.PHONY: shell
shell:                        ## Open a shell inside the container (for debugging)
	$(COMPOSE) run --rm --entrypoint bash pipeline

# ---------------------------------------------------------------------------
# Data directories
# ---------------------------------------------------------------------------

.PHONY: dirs
dirs:                         ## Create local data directories if they don't exist
	mkdir -p invoices output data
	@echo "$(GREEN)Created: invoices/ output/ data/$(RESET)"

# ---------------------------------------------------------------------------
# Ollama sidecar (optional)
# ---------------------------------------------------------------------------

.PHONY: ollama-up
ollama-up:                    ## Start Ollama as a sidecar container
	$(COMPOSE) --profile with-ollama up -d ollama
	@echo "$(CYAN)Ollama started. Pull a model with: make ollama-pull$(RESET)"

.PHONY: ollama-pull
ollama-pull:                  ## Pull the default model into the sidecar Ollama
	$(COMPOSE) exec ollama ollama pull $${OLLAMA_MODEL:-llama3.2}

.PHONY: ollama-down
ollama-down:                  ## Stop the Ollama sidecar container
	$(COMPOSE) --profile with-ollama down

# ---------------------------------------------------------------------------
# Docling model cache
# ---------------------------------------------------------------------------

.PHONY: cache-info
cache-info:                   ## Show the Docling model cache volume info
	docker volume inspect invoice_pipeline_docling-models 2>/dev/null \
	  || echo "Volume not yet created (will be on first run)"

.PHONY: cache-clear
cache-clear:                  ## Delete the Docling model cache (forces re-download)
	@read -p "This will delete the Docling model cache (~1 GB will re-download). Continue? [y/N] " confirm; \
	  [ "$$confirm" = "y" ] && docker volume rm invoice_pipeline_docling-models || echo "Aborted."

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

.PHONY: clean
clean:                        ## Remove stopped containers and dangling images
	docker container prune -f
	docker image prune -f

.PHONY: clean-output
clean-output:                 ## Delete all JSON files and pipeline state from output/
	@read -p "Delete all JSON files and state from output/? [y/N] " confirm; \
	  [ "$$confirm" = "y" ] && rm -f output/*.json output/.pipeline_state.json && echo "output/ cleared." || echo "Aborted."

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help:                         ## Show this help message
	@echo ""
	@echo "$(BOLD)Invoice Processing Pipeline$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-20s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(BOLD)Examples:$(RESET)"
	@echo "  make build                              # build the image"
	@echo "  make check                              # verify Ollama + data files"
	@echo "  make process                            # one-shot batch (invoices/, then exit)"
	@echo "  make watch                              # foreground watch mode (Ctrl-C to stop)"
	@echo "  make up                                 # start pipeline (watch) + dashboard"
	@echo "  make dashboard                          # start dashboard only (port 8080)"
	@echo "  make logs                               # follow all service logs"
	@echo "  make down                               # stop all services"
	@echo "  $(COMPOSE) run --rm pipeline process /app/invoices/my_invoice.pdf"
	@echo "  $(COMPOSE) run --rm pipeline watch /app/invoices --interval 60"
	@echo "  OLLAMA_MODEL=qwen2.5:7b make process    # use a different model"
	@echo "  DASHBOARD_PORT=9090 make up             # dashboard on a custom port"
	@echo ""
