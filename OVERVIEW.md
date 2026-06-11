# Visão Geral do Ambiente — HomeLAB Observabilidade

> **Documento:** referência para quem está conhecendo o ambiente pela primeira vez.
> **Última atualização:** 2026-06-10 — Agente autônomo v2 (`zabbix_agent.py`) implementado: Gemini Flash 2.5 + MCP Zabbix + restart via SSH (conta `svc-zabbix`); webhook na porta 9001; `.env.zabbix-agent.example` adicionado; runbook RB-005
> **Responsável:** Robson Santiago | SRE / Engenheiro de Observabilidade

---

## 1. O que é este ambiente

Este é um **laboratório de observabilidade** rodando em um servidor Proxmox local (HomeLAB). O objetivo é explorar, testar e documentar ferramentas modernas de observabilidade — especialmente o **Grafana 13** e suas capacidades de visualização — integradas a um ambiente de monitoramento Zabbix já existente.

O lab tem dois sistemas de monitoramento coexistindo:

| Sistema | Função | Dados coletados |
|---------|--------|-----------------|
| **Zabbix 7.0** | Monitoramento de infraestrutura (VMs e serviços) | Métricas de host via Agent 2 |
| **Stack LGTM** (Grafana + Prometheus + Loki + Tempo) | Observabilidade de aplicações | Métricas, logs e traces |

Além disso, o ambiente conta com um **MCP Server** que expõe os dados do Zabbix como ferramentas via Model Context Protocol, permitindo consultas em linguagem natural ao Zabbix por agentes de IA compatíveis com MCP. Sobre essa base roda um **agente autônomo de resposta a incidentes** (`zabbix_agent.py`) que recebe webhooks do Zabbix, investiga com o Gemini Flash 2.5 usando as ferramentas MCP e executa ações corretivas dentro de guardrails (ver `AGENT.md`).

---

## 2. Infraestrutura — Proxmox HomeLAB

**Hipervisor:** Proxmox VE — host `192.168.10.254`
**Rede:** `192.168.10.0/24` (rede interna, sem acesso externo)
**Acesso SSH:** chave `~/.ssh/homelab_ed25519` — usuário `fedora` em todas as VMs

### Inventário de VMs

| VM | IP | vCPU | RAM | Disco | OS | Função |
|---|---|---|---|---|---|---|
| `ansible` | 192.168.10.104 | 2 | 6 GB | 32 GB | Fedora 40 | Stack de Observabilidade |
| `docker` | 192.168.10.112 | 4 | 6 GB | 32 GB | Fedora 40 | Minikube / Kubernetes |
| `mcp-server` | 192.168.10.210 | 1 | 2 GB | 32 GB | RHEL 9.7 | MCP Server para Zabbix |
| `zabbix-db` | 192.168.10.201 | 2 | 4 GB | 32 GB | Fedora 40 | Banco de dados do Zabbix |
| `zabbix-server` | 192.168.10.202 | 2 | 4 GB | 32 GB | Fedora 40 | Zabbix Server |
| `zabbix-front` | 192.168.10.203 | 2 | 2 GB | 32 GB | Fedora 40 | Zabbix Frontend |
| `zabbix-proxy` | 192.168.10.204 | 2 | 4 GB | 32 GB | Fedora 40 | Zabbix Proxy |

---

## 3. Arquitetura geral

```
┌─────────────────────────────────────────────────────────────────────┐
│                     HomeLAB — 192.168.10.0/24                       │
│                                                                     │
│  ┌──────────────────────────────────────┐                           │
│  │  VM ansible (192.168.10.104)         │                           │
│  │  Stack de Observabilidade            │◄── SRE / Browser          │
│  │                                      │                           │
│  │  Grafana 13        :3000             │                           │
│  │  Prometheus        :9090            │                           │
│  │  Loki              :3100            │                           │
│  │  Tempo             :3200            │                           │
│  │  OTEL Collector    :4317/:4318 ◄────────── OTLP push (apps K8s) │
│  │  Zabbix Agnt       :10050           │                           │
│  └──────────────────────────────────────┘                          │
│                                         │                           │
│  ┌──────────────────────────────────┐   │                           │
│  │  VM mcp-server (192.168.10.210)  │   │                           │
│  │                                  │   │                           │
│  │  Zabbix MCP Server :8080/mcp ◄───────── Agente de IA (MCP Client) │
│  │  MCP Admin Portal  :9090         │   │                           │
│  │  Zabbix Agnt       :10050        │   │                           │
│  └──────────┬───────────────────────┘   │                           │
│             │ Zabbix API (read-only)    │                           │
│             ▼                           │                           │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Stack Zabbix                                                │   │
│  │                                                              │   │
│  │  zabbix-front  (192.168.10.203) :80 ◄── SRE / Browser        │   │
│  │       │                                                      │   │
│  │       ▼                                                      │   │
│  │  zabbix-server (192.168.10.202) :10051                       │   │
│  │       │                    ▲                                 │   │
│  │       ▼                    │ agentes coletam para cá         │   │
│  │  zabbix-db     (192.168.10.201) :5432 (PostgreSQL)           │   │
│  │                                                              │   │
│  │  zabbix-proxy  (192.168.10.204) :10051 (modo passivo)        │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────┐                               │
│  │  VM docker (192.168.10.112)      │                               │
│  │  Minikube (K8s)                  │◄── futuras métricas/traces    │
│  │  Zabbix Agnt :10050              │                               │
│  └──────────────────────────────────┘                               │
│                                                                     │
│  Zabbix Agent 2 instalado em todas as 7 VMs                          
  ansible/docker/mcp-server → .204 (proxy) → .202 (server)           
  VMs Zabbix → .202 diretamente                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Sistemas em detalhe

### 4.1 Stack de Observabilidade — VM `ansible` (192.168.10.104)

Todos os serviços rodam como **containers Docker** gerenciados por Docker Compose. O projeto vive em `~/observability` nesta VM.

| Serviço | Versão | Porta | URL | Função |
|---------|--------|-------|-----|--------|
| Grafana | 13.0.1 | 3000 | http://192.168.10.104:3000 | Visualização e dashboards |
| Prometheus | v2.52.0 | 9090 | http://192.168.10.104:9090 | Coleta e armazenamento de métricas |
| Loki | 3.0.0 | 3100 | http://192.168.10.104:3100/ready | Agregação de logs |
| Tempo | 2.4.1 | 3200 | http://192.168.10.104:3200/ready | Armazenamento de traces distribuídos |
| OTEL Collector | 0.102.0 | 4317 (gRPC) / 4318 (HTTP) | http://192.168.10.104:13133/ | Receptor central de telemetria (métricas, logs, traces) |
| Promtail | 3.0.0 | 9080 | http://192.168.10.104:9080/ready | Coleta de logs dos containers → Loki |

**Credenciais do Grafana:** usuário `admin` — senha definida no `.env` da VM (`GF_SECURITY_ADMIN_PASSWORD`). Último reset: 2026-05-27 via `grafana cli admin reset-admin-password`.

**Plugin instalado:** `jdbranham-diagram-panel` v1.10.4 — objetivo principal do lab. **Importante:** o plugin usa sintaxe **Mermaid.js**, não GraphViz DOT (ver `graphviz_guide.md`).

**Datasources provisionados via código** (`config/grafana/provisioning/datasources/`) com UIDs fixos:
- Prometheus (`PBFA97CFB590B2093`) → PromQL
- Loki (`P8E80F9AEF21F6940`) → LogQL
- Tempo (`tempo`) → TraceQL

**Dashboards disponíveis** (pasta Lab):
- `graphviz-testes` — topologia do lab em Mermaid
- `kubernetes-overview` — estado do cluster Minikube via kube-state-metrics
- `logs-overview` — logs ao vivo de todos os containers via Promtail/Loki

**Como operar:**
```bash
ssh 192.168.10.104
cd ~/observability
make up       # sobe todos os serviços
make status   # verifica estado dos containers
make health   # valida endpoints
make logs     # acompanha logs em tempo real
```

---

### 4.2 Stack Zabbix — VMs `.201` a `.204`

Arquitetura de 3 camadas independente da stack de observabilidade. **Zabbix 7.0.24.**

| VM | IP | Serviço | Porta | Acesso |
|---|---|---|---|---|
| `zabbix-db` | 192.168.10.201 | PostgreSQL | 5432 | interno |
| `zabbix-server` | 192.168.10.202 | Zabbix Server | 10051 | interno |
| `zabbix-front` | 192.168.10.203 | Frontend (Apache) | 80 | http://192.168.10.203/zabbix/ |
| `zabbix-proxy` | 192.168.10.204 | Zabbix Proxy (passivo) | 10051 | interno |

**Credenciais do Zabbix Frontend:** acesso em `http://192.168.10.203/zabbix/` — usuário `Admin`.

**Fluxo de dados:**
```
Zabbix Agent 2 (ansible / docker / mcp-server)
        │
        ▼ porta 10050 → 10051
Zabbix Proxy (192.168.10.204)   ← coleta ansible, docker, mcp-server
        │
        ▼
Zabbix Server (192.168.10.202)  ← coleta VMs da própria stack Zabbix
        │
        ├──► Zabbix DB (192.168.10.201) — persiste dados
        └──► Zabbix Frontend (192.168.10.203) — visualização
```

**Hosts monitorados no Zabbix:**

| Host Zabbix | IP | ID | Template | Origem |
|---|---|---|---|---|
| zabbix-server | 127.0.0.1 | 10084 | Linux by Zabbix agent | pré-existente |
| zabbix-db-banco | 192.168.10.201 | 10680 | Linux by Zabbix agent | pré-existente |
| zabbix-db-host | 192.168.10.201 | 10681 | Linux by Zabbix agent | pré-existente |
| zabbix-proxy | 192.168.10.204 | 10682 | Linux by Zabbix agent | pré-existente |
| proxmox | 192.168.10.254 | 10683 | Linux by Zabbix agent | pré-existente |
| MCP-Server | 192.168.10.210 | 10684 | Linux by Zabbix agent | adicionado 2026-05-25 |
| zabbix-front | 192.168.10.203 | 10685 | Linux by Zabbix agent | adicionado 2026-05-25 |
| docker | 192.168.10.112 | 10686 | Linux by Zabbix agent | adicionado 2026-05-25 |
| ansible | 192.168.10.104 | 10687 | Linux by Zabbix agent | adicionado 2026-05-25 |

---

### 4.3 MCP Server — VM `mcp-server` (192.168.10.210)

Dois servidores MCP independentes rodando via systemd:

#### Zabbix MCP Server (porta 8080)

Expõe a API do Zabbix como **237 ferramentas MCP**, permitindo que agentes de IA consultem e analisem dados do Zabbix em linguagem natural.

| Endpoint | URL | Função |
|----------|-----|--------|
| MCP endpoint | http://192.168.10.210:8080/mcp | Conexão de clientes MCP |
| Health check | http://192.168.10.210:8080/health | `{"status":"ok"}` |
| Admin portal | http://192.168.10.210:9090/ | Gerenciamento e tokens |

**Software:** `initMAX/zabbix-mcp-server` v1.30 — systemd.
**Configuração:** `/etc/zabbix-mcp/config.toml` + `/etc/zabbix-mcp/.env` (chmod 600).

#### Kubernetes MCP Server (porta 8081)

Expõe o cluster Minikube (192.168.10.112) como ferramentas MCP, modo read-only.

| Endpoint | URL | Função |
|----------|-----|--------|
| MCP endpoint | http://192.168.10.210:8081/mcp | Conexão de clientes MCP |
| Health check | http://192.168.10.210:8081/healthz | `200 OK` |
| Stats | http://192.168.10.210:8081/stats | Métricas de uso |

**Software:** `containers/kubernetes-mcp-server` v0.0.62 — systemd (`kubernetes-mcp-server.service`).
**Configuração:** `/etc/kubernetes-mcp/config.toml` + `/etc/kubernetes-mcp/kubeconfig` (chmod 600).
**Toolsets:** `core` + `config`. Secrets e ServiceAccounts bloqueados.
**Runbook:** `docs/runbooks/RB-004-instalacao-kubernetes-mcp-server.md`.

---

### 4.4 VM docker (192.168.10.112)

Host de workloads Kubernetes via **Minikube**. Será a **fonte de dados de telemetria** (métricas, logs, traces) que alimenta a stack de observabilidade no `ansible`.

**Estrutura de manifestos K8s:** `/home/fedora/k8s-obs/`

| App | Stack | Status |
|-----|-------|--------|
| `painel-estudos-sre` | Python · FastAPI · SQLite | ✅ Imagem `v1.2` rodando (`1/1 Running`), acessível via port-forward em `http://192.168.10.112:8080/` |
| `focustrack` | Python · FastAPI · Supabase · Gemini AI | ⏳ Manifestos pendentes (Dockerfile pronto) |

**kube-state-metrics** instalado via Helm (`prometheus-community/kube-state-metrics` v2.19.0) no namespace `kube-system`. Métricas expostas via port-forward em `http://192.168.10.112:32080/metrics` e coletadas pelo Prometheus da VM `ansible`.

**Port-forwards persistentes via systemd** (instalados em 2026-05-29):

| Serviço systemd | Porta | Destino |
|-----------------|-------|---------|
| `minikube-start.service` | — | Inicia o Minikube no boot (`After=docker.service`) |
| `painel-estudos-sre-portforward.service` | 8080 | `svc/painel-estudos-sre-service` → pod:80 |
| `kube-state-metrics-portforward.service` | 32080 | `svc/kube-state-metrics` → pod:8080 |

**DNAT persistente:** `minikube-apiserver-dnat.service` expõe o apiserver (`192.168.10.112:8443 → 192.168.49.2:8443`) necessário para o Kubernetes MCP Server.

**Fluxo ativo:** kube-state-metrics → Prometheus (ansible) → Grafana (dashboard `kubernetes-overview`).

---

### 4.5 Agente Autônomo de Incidentes — `zabbix_agent.py` (v2.0.0)

Agente de resposta a incidentes que roda na **workstation** (`192.168.10.108`), fora das VMs do lab. Recebe webhooks do Zabbix, investiga de forma autônoma e age dentro dos guardrails do `AGENT.md`.

| Atributo | Valor |
|----------|-------|
| Implementação | `scripts/zabbix_agent.py` (v2.0.0) |
| Modelo | Gemini Flash 2.5 (free tier: 1.500 req/dia) |
| Webhook | `http://192.168.10.108:9001` |
| Ferramentas | 14 ferramentas do MCP Zabbix + `ssh_execute` (customizada) |
| Notificação | Telegram |
| Configuração | `.env.zabbix-agent` (template: `.env.zabbix-agent.example`) |

**Fluxo:** Trigger Zabbix → Action → Media Type → webhook → aguarda 60s (descarta se resolver sozinho) → acknowledge → Gemini investiga (`host_get` → `ssh_execute restart` → `problem_active_get`) → Telegram.

**Ação autônoma principal:** ao detectar `agent is not available`, reinicia o `zabbix-agent2` no host afetado via SSH como a conta de serviço `svc-zabbix` (sudo NOPASSWD restrito a `systemctl {start,stop,restart,status} zabbix-agent2`). A conta existe nas 6 VMs do inventário (104, 112, 210, 202, 203, 204).

> Predecessor: `docs/archive/agent_orchestrator.py` (v1) — webhook → Claude API + MCP **remoto** → Telegram/Google Chat. Protótipo aposentado (MCP remoto não alcança IP privado do HomeLAB); nunca rodou em produção. Arquivado como registro histórico; o `zabbix_agent.py` é a implementação ativa. Setup completo na seção 17 do `docs/setup-guide.md`.

---

## 5. Zabbix Agent 2 — cobertura de monitoramento

**Todas as 7 VMs** têm o Zabbix Agent 2 v7.0.26 instalado. A partir de 2026-05-27, `ansible`, `docker` e `mcp-server` reportam para o **proxy** (192.168.10.204); as VMs da stack Zabbix mantêm configuração original (Server=192.168.10.202):

| VM | IP | Hostname do agente | Porta | Server (Agent2) | Observações |
|---|---|---|---|---|---|
| ansible | 192.168.10.104 | `ansible` | 10050 | **192.168.10.204** | pacote `el9`; firewalld aberto |
| docker | 192.168.10.112 | `docker` | 10050 | **192.168.10.204** | pacote `el9`; firewalld desabilitado |
| mcp-server | 192.168.10.210 | `MCP-Server` | 10050 | **192.168.10.204** | pacote `el9`; RHEL 9.7; SELinux OK |
| zabbix-db | 192.168.10.201 | `zabbix-db` | 10050 | 192.168.10.202 | pré-instalado |
| zabbix-server | 192.168.10.202 | `zabbix-server` | 10050 | 127.0.0.1 | pré-instalado; auto-monitoramento |
| zabbix-front | 192.168.10.203 | `zabbix-front` | 10050 | 192.168.10.202 | pacote `el9`; repo corrigido de rhel/10 |
| zabbix-proxy | 192.168.10.204 | `zabbix-proxy` | 10050 | 192.168.10.202 | pré-instalado |

> **Atenção — CPU kvm64:** VMs Fedora 40 com CPU tipo `kvm64` no Proxmox não executam binários compilados para RHEL 10 (`el10`). Usar sempre o pacote `el9` nessas VMs.

---

## 6. Integrações entre sistemas

```
Agente de IA (MCP Client)
    │
    │ MCP Protocol (Bearer token)
    ▼
Zabbix MCP Server (192.168.10.210:8080)
    │
    │ Zabbix API — read-only
    ▼
Zabbix Frontend / API (192.168.10.203/zabbix)
    │
    ▼
Zabbix Server (192.168.10.202)
    │
    ├──► PostgreSQL (192.168.10.201) — dados históricos
    └──► Zabbix Agent 2 em todas as VMs — coleta de métricas


apps no Minikube (OTLP push)
    │
    ▼ 4317/4318
OTEL Collector (192.168.10.104)
    ├──► Prometheus (192.168.10.104:9090) — métricas via remote_write
    ├──► Loki       (192.168.10.104:3100) — logs via Loki push API
    └──► Tempo      (192.168.10.104:3200) — traces via OTLP interno

Grafana 13 (192.168.10.104:3000)
    ├──► Prometheus — PromQL
    ├──► Loki       — LogQL
    └──► Tempo      — TraceQL


Agente Autônomo (workstation 192.168.10.108:9001)
    ▲ webhook
    │
Zabbix Action / Media Type (192.168.10.202/203)
    │
    └──► zabbix_agent.py
             ├──► Gemini Flash 2.5 (function calling)
             ├──► Zabbix MCP Server (192.168.10.210:8080) — diagnóstico + acknowledge
             ├──► SSH svc-zabbix@<host> — restart zabbix-agent2
             └──► Telegram — notificação

[Fase 3 — planejado]
Prometheus ◄── Zabbix Exporter ◄── Zabbix Server
```

---

## 7. Estado atual e roadmap

### Fase 1 — Concluída ✅
- Stack LGTM operacional (Grafana 13 + Prometheus + Loki + Tempo)
- Zabbix 7.0 monitorando todas as 7 VMs via Agent 2
- MCP Server integrado via protocolo MCP (237 ferramentas Zabbix)
- Plugin GraphViz instalado no Grafana (experimentos em andamento)
- SSH por chave configurado em todas as VMs

### Fase 2 — Em andamento 🔄
- **OTEL Collector** ✅ — receptor central de telemetria (4317/4318) — rota OTLP → Prometheus/Loki/Tempo
- **Promtail** ✅ — coleta de logs dos containers Docker → Loki
- **kube-state-metrics** ✅ — métricas do cluster Kubernetes → Prometheus
- **Dashboard Kubernetes** ✅ — estado do cluster no Grafana
- **Dashboard Logs** ✅ — logs ao vivo de todos os serviços no Grafana
- **`painel-estudos-sre` no Minikube** ✅ — imagem `v1.2` rodando, port-forward persistente (:8080)
- **Port-forwards e DNAT persistentes** ✅ — serviços systemd na VM `docker` (2026-05-29)
- **Kubernetes MCP Server** ✅ — v0.0.62 instalado em `mcp-server:8081` (2026-05-29)
- **AGENT.md** ✅ — especificação do agente autônomo de SRE (guardrails, fluxo, criticidade por host); atualizado para v2.0.0 (2026-06-10)
- **agent_orchestrator.py** 📦 — agente v1 (protótipo Claude API + MCP remoto); aposentado e movido para `docs/archive/` em 2026-06-11 (nunca rodou; MCP remoto incompatível com rede privada)
- **zabbix_agent.py** ✅ — agente v2: webhook → Gemini Flash 2.5 + MCP Zabbix + restart via SSH (`svc-zabbix`) → Telegram; ack após 60s (2026-06-10)
- **`focustrack` no Minikube** — manifestos K8s + instrumentação OTEL
- Alertmanager (roteamento de alertas)
- Node Exporter (métricas de host para o Prometheus)

### Fase 3 — Futura 🗓️
- Zabbix Exporter (Zabbix → Prometheus)
- cAdvisor (métricas de containers)
- Mimir (armazenamento de longo prazo de métricas)
- Ansible automation para provisionamento dos hosts

---

## 8. Acesso rápido — URLs e endpoints

| Sistema | URL / Acesso | Credenciais |
|---------|-------------|-------------|
| **Grafana** | http://192.168.10.104:3000 | ver `.env` na VM ansible |
| **Prometheus** | http://192.168.10.104:9090 | sem autenticação (lab) |
| **Loki** | http://192.168.10.104:3100/ready | sem autenticação (lab) |
| **Tempo** | http://192.168.10.104:3200/ready | sem autenticação (lab) |
| **OTEL Collector** (health) | http://192.168.10.104:13133/ | sem autenticação (lab) |
| **OTEL Collector** (gRPC) | 192.168.10.104:4317 | endpoint OTLP para apps |
| **OTEL Collector** (HTTP) | http://192.168.10.104:4318 | endpoint OTLP para apps |
| **Zabbix Frontend** | http://192.168.10.203/zabbix/ | usuário `Admin` |
| **MCP Admin** | http://192.168.10.210:9090/ | usuário `admin` |
| **Zabbix MCP endpoint** | http://192.168.10.210:8080/mcp | Bearer token (`.env.mcp-server`) |
| **Kubernetes MCP endpoint** | http://192.168.10.210:8081/mcp | sem autenticação (lab, read-only) |

**SSH:**
```bash
ssh 192.168.10.104   # ansible  — stack de observabilidade
ssh 192.168.10.112   # docker   — Minikube
ssh 192.168.10.210   # mcp-server
ssh 192.168.10.201   # zabbix-db
ssh 192.168.10.202   # zabbix-server
ssh 192.168.10.203   # zabbix-front
ssh 192.168.10.204   # zabbix-proxy
```
> Pré-requisito: ter a chave `~/.ssh/homelab_ed25519` configurada localmente.

---

## 9. Guia de arquivos do projeto

| Arquivo | Para que serve |
|---------|----------------|
| `OVERVIEW.md` | **Este arquivo** — visão geral do ambiente |
| `README.md` | Como operar a stack de observabilidade (make up, comandos, healthcheck) |
| `docs/setup-guide.md` | Guia completo de setup do zero ao ambiente (público) |
| `docs/hosts.md` | Inventário técnico detalhado de cada VM |
| `CHANGELOG.md` | Histórico completo de todas as alterações do ambiente — não versionado (`.gitignore`) |
| `SECURITY.md` | Políticas e checklist de segurança pré-push |
| `graphviz_guide.md` | Documentação do experimento GraphViz no Grafana 13 |
| `CLAUDE.md` | Instruções locais para agentes de IA — não versionado (`.gitignore`) |
| `AGENT.md` | Especificação do agente autônomo de SRE — guardrails, fluxo, inventário de criticidade |
| `docs/archive/agent_orchestrator.py` | Agente v1 — protótipo Claude API + MCP remoto, aposentado (registro histórico) |
| `scripts/zabbix_agent.py` | Agente v2 (ativo) — webhook → Gemini Flash + MCP Zabbix + SSH → Telegram |
| `.env.example` | Template das variáveis da stack de observabilidade |
| `.env.mcp-server.example` | Template das variáveis do MCP Server |
| `.env.agent.example` | Template das variáveis do agente orquestrador v1 (API key, tokens, webhook secret) |
| `.env.zabbix-agent.example` | Template das variáveis do agente de incidentes v2 (Gemini, MCP, Telegram, SSH) |
| `docs/postmortem/postmortem.md` | Template de postmortem blameless — copiar para `INC-YYYY-MM-DD-001.md` |
| `docs/runbooks/RB-001-stack-observabilidade.md` | RB-001: Stack não sobe — árvore de decisão por sintoma |
| `docs/runbooks/RB-002-stack-observabilidade.md` | RB-002: Container caído no ansible — passo a passo sequencial |
| `docs/runbooks/RB-003-zabbix-agent-indisponivel.md` | RB-003: Zabbix Agent indisponível — qualquer host do HomeLAB |
| `docs/runbooks/RB-004-instalacao-kubernetes-mcp-server.md` | RB-004: Instalação/reinstalação do Kubernetes MCP Server |
| `docs/runbooks/RB-005-servicos-k8s-docker-vm.md` | RB-005: Serviços K8s offline na VM docker (painel, port-forwards, DNAT) |

---

## 10. Observações operacionais

- **Segredos:** nunca versionados. Ficam em `.env`, `.env.ssh`, `.env.mcp-server`, `.env.agent` e `.env.zabbix-agent` (todos no `.gitignore`). Credenciais do MCP Server ficam em `/etc/zabbix-mcp/.env` na VM remota.
- **Reprodutibilidade:** a stack de observabilidade sobe do zero com `make up` na VM `ansible`.
- **Configuração como código:** datasources e configurações do Grafana são provisionados via arquivos em `config/grafana/provisioning/`. Dashboards são gerenciados via API (não file provisioning — bug `allowUiUpdates` no Grafana 13). JSONs dos dashboards ficam em `config/grafana/dashboards/` como fonte de verdade; importar com `scripts/import-dashboards.sh` em setup fresh.
- **Plugin GraphViz:** este lab existe principalmente para testar o plugin `jdbranham-diagram-panel` v1.10.4 no Grafana 13. Resultados documentados em `graphviz_guide.md`.
- **Troubleshooting:** cinco runbooks disponíveis em `docs/runbooks/` — RB-001 (stack não sobe), RB-002 (container caído), RB-003 (Zabbix Agent indisponível), RB-004 (Kubernetes MCP Server), RB-005 (serviços K8s offline na VM docker). Para incidentes maiores, usar o template de postmortem em `docs/postmortem/postmortem.md`.
- **Agente autônomo:** o `zabbix_agent.py` trata automaticamente o cenário do RB-003 (Zabbix Agent indisponível) reiniciando o serviço via SSH antes de escalar. Roda na workstation, não nas VMs.
