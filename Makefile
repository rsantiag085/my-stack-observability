.PHONY: up down restart logs status validate health reset lint

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

status:
	docker compose ps

validate:
	yamllint config/

health:
	./scripts/health-check.sh

reset:
	@read -p "ATENCAO: Esta operacao destroi volumes e dados. Confirma? [y/N] " confirm && \
	[ "$$confirm" = "y" ] && ./scripts/reset-lab.sh || echo "Operacao cancelada."

lint:
	yamllint config/
