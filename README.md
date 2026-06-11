# Lab de Observabilidade — Grafana 13

> **Status:** Funcional — Fase 2 em andamento
> **Objetivo:** Lab de observabilidade com Grafana 13, Prometheus, Loki, Tempo e OTEL Collector — integrado ao Zabbix (3 camadas) e Minikube.
> **Novo no ambiente?** Comece pelo [`OVERVIEW.md`](OVERVIEW.md) — visão completa de todos os sistemas.
> **Quer montar do zero?** Siga o [`docs/setup-guide.md`](docs/setup-guide.md) — guia passo a passo de toda a stack.

---

## Infraestrutura — HomeLAB Proxmox (`192.168.10.0/24`)

| VM            | IP                | Função                                      |
|---------------|-------------------|---------------------------------------------|
| ansible       | 192.168.10.104    | Stack de Observabilidade (este projeto)     |
| docker        | 192.168.10.112    | Minikube / workloads Kubernetes             |
| mcp-server    | 192.168.10.210    | Zabbix MCP Server (:8080) + K8s MCP Server (:8081) |
| zabbix-db     | 192.168.10.201    | Banco de dados do Zabbix                    |
| zabbix-server | 192.168.10.202    | Zabbix Server                               |
| zabbix-front  | 192.168.10.203    | Zabbix Frontend                             |
| zabbix-proxy  | 192.168.10.204    | Zabbix Proxy                                |

> Inventário completo em `docs/hosts.md` (arquivo local — no `.gitignore`).

---

## Pré-requisitos

| Ferramenta     | Versão mínima | Verificar                |
|----------------|---------------|--------------------------|
| Docker         | 24.x          | `docker --version`       |
| Docker Compose | 2.x (plugin)  | `docker compose version` |
| Make           | 4.x           | `make --version`         |
| yamllint       | 1.x           | `yamllint --version`     |

---

## Acesso SSH às VMs

Todas as VMs usam autenticação por chave (`~/.ssh/homelab_ed25519`, sem passphrase):

```bash
ssh 192.168.10.104   # ansible
ssh 192.168.10.112   # docker
ssh 192.168.10.210   # mcp-server
ssh 192.168.10.201   # zabbix-db
ssh 192.168.10.202   # zabbix-server
ssh 192.168.10.203   # zabbix-front
ssh 192.168.10.204   # zabbix-proxy
```

> IPs e usuário documentados em `.env.ssh` (local, no `.gitignore`). Usuário padrão: `fedora`.

---

## Como subir o lab

```bash
# 1. Clonar e entrar no diretório (na VM ansible — 192.168.10.104)
git clone <repo-url> my-stack-observability && cd my-stack-observability

# 2. Configurar variáveis de ambiente
cp .env.example .env
# Editar .env com valores reais (senhas, etc.)

# 3. Subir todos os serviços
make up

# 4. Verificar saúde dos serviços
make health
```

---

## Serviços e portas — Fase atual

| Serviço        | Porta      | URL de acesso                        |
|----------------|------------|--------------------------------------|
| Grafana 13     | 3000       | http://192.168.10.104:3000           |
| Prometheus     | 9090       | http://192.168.10.104:9090           |
| Loki           | 3100       | http://192.168.10.104:3100/ready     |
| Tempo          | 3200       | http://192.168.10.104:3200/ready     |
| OTEL Collector | 4317 (gRPC) / 4318 (HTTP) | http://192.168.10.104:13133/ (health) |
| Promtail       | 9080       | http://192.168.10.104:9080/ready     |

---

## Fases do lab

| Fase | Status      | Componentes                                                                  |
|------|-------------|------------------------------------------------------------------------------|
| 1    | ✅ Concluída | Grafana 13 · Prometheus · Loki · Tempo                                        |
| 2    | 🔄 Em andamento | + OTEL Collector ✅ · Promtail ✅ · kube-state-metrics ✅ · Alertmanager · Node Exporter |
| 3    | 🗓️ Futura   | + Zabbix Exporter · cAdvisor · Mimir · Ansible                               |

---

## Comandos disponíveis

```bash
make up        # Sobe todos os serviços
make down      # Para todos os serviços
make restart   # Reinicia todos os serviços
make logs      # Acompanha logs em tempo real
make status    # Lista o estado de cada container
make health    # Valida endpoints de todos os serviços
make validate  # Valida arquivos YAML de configuração
make lint      # Executa yamllint nos configs
make reset     # DESTRUTIVO: destrói volumes e recria do zero (pede confirmação)
```

> **Dashboards após setup fresh:** após `make up` do zero, importar os dashboards manualmente:
> ```bash
> GRAFANA_PASS=<senha> ./scripts/import-dashboards.sh
> ```
> Os JSONs ficam em `config/grafana/dashboards/` como fonte de verdade no repositório.

---

## Estrutura de diretórios

```
observability/
├── OVERVIEW.md                    ← Visão geral completa do ambiente (comece aqui)
├── CLAUDE.md
├── README.md
├── CHANGELOG.md                   ← Histórico de mudanças (local — no .gitignore)
├── SECURITY.md
├── graphviz_guide.md
├── Makefile
├── AGENT.md                       ← Especificação do agente autônomo de SRE
├── .env.example                   ← Template das variáveis do Grafana/stack
├── .env.mcp-server.example        ← Template das variáveis do MCP Server
├── .env.agent.example             ← Template das variáveis do agente orquestrador
├── .env.zabbix-agent.example      ← Template das variáveis do agente de incidentes Zabbix
├── .env                           ← Valores reais (no .gitignore)
├── .env.ssh                       ← IPs e usuário SSH das VMs (no .gitignore)
├── .env.mcp-server                ← Valores reais do MCP Server (no .gitignore)
├── docker-compose.yml
├── docker-compose.override.yml
├── config/
│   ├── prometheus/                ← prometheus.yml + rules/
│   ├── grafana/                   ← grafana.ini + provisioning/ + dashboards/
│   │   └── dashboards/            ← graphviz-testes.json · kubernetes-overview.json · logs-overview.json
│   ├── loki/
│   ├── tempo/
│   ├── otel/                      ← otel-collector.yml
│   └── promtail/                  ← promtail.yml
├── scripts/
│   ├── health-check.sh
│   ├── reset-lab.sh
│   ├── import-dashboards.sh   ← Importa dashboards JSON para o Grafana via API (setup fresh)
│   └── zabbix_agent.py        ← Agente autônomo de incidentes Zabbix (Gemini Flash + MCP → SSH → Telegram)
└── docs/
    ├── setup-guide.md         ← Guia completo de setup do zero (público)
    ├── homelab-diagram.png    ← Diagrama ASCII da arquitetura (estilo matricial)
    ├── gen_diagram.py         ← Script para gerar homelab-diagram.png (Pillow)
    ├── homelab-diagram.dot    ← Fonte Graphviz do diagrama (referência)
    ├── hosts.md               ← Inventário do HomeLAB Proxmox (IPs internos — local)
    ├── archive/               ← Protótipos aposentados (ex: agent_orchestrator.py v1)
    ├── postmortem/            ← Postmortems de incidentes (local)
    └── runbooks/              ← Runbooks de troubleshooting RB-001 a RB-004 (local)
```

---

## Documentação

| Documento | Localização | Conteúdo |
|-----------|-------------|----------|
| Setup Guide | [`docs/setup-guide.md`](docs/setup-guide.md) | Guia passo a passo completo: Proxmox → Docker → LGTM → Zabbix → Minikube → MCP |
| Diagrama | [`docs/homelab-diagram.png`](docs/homelab-diagram.png) | Arquitetura visual de todo o lab |
| Visão Geral | [`OVERVIEW.md`](OVERVIEW.md) | Estado atual do ambiente, decisões e próximos passos |
| GraphViz Guide | [`graphviz_guide.md`](graphviz_guide.md) | Testes do plugin GraphViz no Grafana 13 |
| Agente SRE | [`AGENT.md`](AGENT.md) | Especificação do agente autônomo de SRE |
| Segurança | [`SECURITY.md`](SECURITY.md) | Política de segurança do lab |

---

## Agente Autônomo de Incidentes Zabbix

`scripts/zabbix_agent.py` — recebe webhooks do Zabbix, investiga via Gemini Flash 2.5 + MCP Zabbix e age autonomamente dentro dos guardrails definidos em `AGENT.md`.

| Componente | Detalhe |
|---|---|
| Modelo | Gemini Flash 2.5 (free tier: 1.500 req/dia) |
| Webhook | `http://192.168.10.108:9001` (porta configurável) |
| MCP Zabbix | `http://192.168.10.210:8080/mcp` (12 ferramentas expostas) |
| Notificação | Telegram |
| Acknowledge | Após 60s de persistência do incidente |
| Restart remoto | Via SSH como `svc-zabbix` (NOPASSWD restrito) |

```bash
# Dependências
pip install mcp google-generativeai httpx python-dotenv

# Configurar
cp .env.zabbix-agent.example .env.zabbix-agent
# editar .env.zabbix-agent

# Subir em background
nohup python scripts/zabbix_agent.py --mode server >> logs/zabbix-agent.log 2>&1 &

# Modo teste (simula incidente sem webhook)
python scripts/zabbix_agent.py --mode test
```

> Spec completa, guardrails e inventário de hosts em [`AGENT.md`](AGENT.md).

---

## Runbooks de troubleshooting

> Os runbooks ficam em `docs/runbooks/` (local — no `.gitignore` por conter IPs e detalhes internos).

| Runbook | Arquivo local | Quando usar |
|---------|---------------|-------------|
| RB-001 | `docs/runbooks/RB-001-stack-observabilidade.md` | Stack não sobe ou está degradada (árvore de decisão por sintoma) |
| RB-002 | `docs/runbooks/RB-002-stack-observabilidade.md` | Container específico caído ou unhealthy (passo a passo) |
| RB-003 | `docs/runbooks/RB-003-zabbix-agent-indisponivel.md` | Zabbix Agent indisponível em qualquer host do HomeLAB |
| RB-004 | `docs/runbooks/RB-004-instalacao-kubernetes-mcp-server.md` | Instalação/reinstalação do Kubernetes MCP Server na VM mcp-server |
| RB-005 | `docs/runbooks/RB-005-servicos-k8s-docker-vm.md` | Serviços K8s offline na VM docker (painel-estudos-sre, port-forwards, DNAT) |

---

## Healthcheck do lab

Após `make up` na VM `ansible` (192.168.10.104):

- [ ] Prometheus: http://192.168.10.104:9090 → `Status: up`
- [ ] Grafana: http://192.168.10.104:3000 → login `admin`
- [ ] Loki: http://192.168.10.104:3100/ready → `ready`
- [ ] Tempo: http://192.168.10.104:3200/ready → `ready`
- [ ] OTEL Collector: http://192.168.10.104:13133/ → `{"status":"Server is healthy."}`
- [ ] Todos os targets do Prometheus em estado `UP`
- [ ] Datasources no Grafana com status `OK`

---

## Integração com Zabbix

O Zabbix roda em arquitetura de 3 camadas no Proxmox:

```
zabbix-proxy (192.168.10.204) → zabbix-server (192.168.10.202) → zabbix-db (192.168.10.201)
                                         ↑
                                 zabbix-front (192.168.10.203)
```

**Coleta via proxy:** os Zabbix Agents nas VMs `ansible`, `docker` e `mcp-server` reportam para o `zabbix-proxy` (192.168.10.204), que encaminha ao Zabbix Server (192.168.10.202). As VMs da stack Zabbix mantêm configuração original.

A integração Zabbix → Prometheus será feita via `zabbix_exporter` na Fase 3.
O job está preparado (comentado) em `config/prometheus/prometheus.yml`.

---

## MCP Servers — VM `mcp-server` (192.168.10.210)

A VM `mcp-server` hospeda dois servidores MCP independentes:

### Zabbix MCP Server (porta 8080)

`initMAX/zabbix-mcp-server` v1.30 — expõe 237 ferramentas do Zabbix via Model Context Protocol.

| Endpoint        | URL                                   | Função                         |
|-----------------|---------------------------------------|--------------------------------|
| MCP endpoint    | `http://192.168.10.210:8080/mcp`      | Conexão de clientes MCP        |
| Health check    | `http://192.168.10.210:8080/health`   | Verificação de saúde           |
| Admin portal    | `http://192.168.10.210:9090/`         | Gerenciamento do servidor      |

**Autenticação:** Bearer token configurado no cliente MCP. Segredos em `/etc/zabbix-mcp/.env` (chmod 600). Template em `.env.mcp-server.example`.

### Kubernetes MCP Server (porta 8081)

`containers/kubernetes-mcp-server` v0.0.62 — expõe o cluster Minikube (192.168.10.112) via MCP, modo read-only.

| Endpoint        | URL                                   | Função                         |
|-----------------|---------------------------------------|--------------------------------|
| MCP endpoint    | `http://192.168.10.210:8081/mcp`      | Conexão de clientes MCP        |
| Health check    | `http://192.168.10.210:8081/healthz`  | `200 OK` (body vazio)          |
| Stats           | `http://192.168.10.210:8081/stats`    | Métricas de uso do servidor    |

**Toolsets ativos:** `core` (pods, events, namespaces), `config` (kubeconfig/contextos). Secrets e ServiceAccounts bloqueados. Configuração em `/etc/kubernetes-mcp/` na VM. Instalação documentada em `docs/runbooks/RB-004-instalacao-kubernetes-mcp-server.md`.
