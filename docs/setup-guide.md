# Guia de Setup — Lab de Observabilidade HomeLAB

> **Para quem é este guia:** qualquer pessoa que precise recriar este ambiente do zero ou entender por que cada peça foi configurada desta forma. Não pressupõe experiência prévia com a stack, mas também não omite detalhes técnicos.
>
> **O que você vai construir:** uma stack LGTM completa (Loki · Grafana · Tempo · Mimir-free via Prometheus) rodando em Docker Compose, integrada ao Zabbix 7.0 existente e ao Minikube, com dois MCP Servers que expõem o Zabbix e o Kubernetes como ferramentas para agentes de IA, e um agente autônomo de incidentes (Gemini Flash 2.5 + MCP Zabbix + SSH).
>
> **Última atualização:** 2026-06-10

---

## Índice

1. [Arquitetura e visão geral](#1-arquitetura-e-visão-geral)
2. [Pré-requisitos](#2-pré-requisitos)
3. [Infraestrutura — VMs no Proxmox](#3-infraestrutura--vms-no-proxmox)
4. [SSH e acesso às VMs](#4-ssh-e-acesso-às-vms)
5. [Docker e Docker Compose — VM ansible](#5-docker-e-docker-compose--vm-ansible)
6. [Estrutura de diretórios e variáveis de ambiente](#6-estrutura-de-diretórios-e-variáveis-de-ambiente)
7. [docker-compose.yml — linha a linha](#7-docker-composeyml--linha-a-linha)
8. [Prometheus — coleta e armazenamento de métricas](#8-prometheus--coleta-e-armazenamento-de-métricas)
9. [Loki — agregação de logs](#9-loki--agregação-de-logs)
10. [Promtail — coleta de logs dos containers](#10-promtail--coleta-de-logs-dos-containers)
11. [OTEL Collector — receptor central de telemetria](#11-otel-collector--receptor-central-de-telemetria)
12. [Tempo — traces distribuídos](#12-tempo--traces-distribuídos)
13. [Grafana 13 — visualização e dashboards](#13-grafana-13--visualização-e-dashboards)
14. [Zabbix — monitoramento de infraestrutura](#14-zabbix--monitoramento-de-infraestrutura)
15. [Kubernetes — Minikube na VM docker](#15-kubernetes--minikube-na-vm-docker)
16. [MCP Servers — Zabbix e Kubernetes para IA](#16-mcp-servers--zabbix-e-kubernetes-para-ia)
17. [Agente Autônomo de Incidentes — zabbix_agent.py](#17-agente-autônomo-de-incidentes--zabbix_agentpy)
18. [Subindo tudo e validando](#18-subindo-tudo-e-validando)
19. [Problemas conhecidos e decisões de design](#19-problemas-conhecidos-e-decisões-de-design)

---

## 1. Arquitetura e visão geral

Este lab tem **duas camadas de monitoramento** coexistindo:

| Camada | Ferramenta | O que monitora | Protocolo de coleta |
|--------|------------|----------------|---------------------|
| Infraestrutura | Zabbix 7.0 | VMs, CPU, memória, disco, rede | Zabbix Agent 2 (pull) |
| Aplicações | LGTM Stack | Métricas, logs, traces de apps | Prometheus (pull) + OTLP (push) |

A razão de ter as duas: o Zabbix já existia no ambiente e cobre muito bem o monitoramento de hosts. A stack LGTM complementa com observabilidade de aplicações (o que o Zabbix não faz bem) e com o Grafana como painel unificado.

```
┌────────────────────────────────────────────────────────────────┐
│                   HomeLAB — 192.168.10.0/24                    │
│                                                                │
│  VM ansible (192.168.10.104) — Stack de Observabilidade        │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Grafana 13 :3000 ◄── datasources: Prometheus/Loki/Tempo│   │
│  │  Prometheus :9090 ◄── scrape: grafana, loki, tempo, ksm │   │
│  │  Loki       :3100 ◄── Promtail (logs containers)         │   │
│  │  Tempo      :3200 ◄── OTLP push das apps                 │   │
│  │  OTEL Col.  :4317/:4318 ◄── apps K8s enviam traces/métricas│ │
│  │  Promtail   :9080 ◄── Docker socket → logs → Loki        │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                │
│  VM docker (192.168.10.112) — Kubernetes (Minikube)            │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Minikube → painel-estudos-sre (app FastAPI + SQLite)    │  │
│  │  kube-state-metrics → Prometheus na VM ansible           │  │
│  │  port-forwards e DNAT via systemd (persistentes no boot) │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  VM mcp-server (192.168.10.210) — MCP Servers                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Zabbix MCP Server     :8080 → API Zabbix (read)         │  │
│  │  Kubernetes MCP Server :8081 → Minikube (read-only)      │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  Stack Zabbix (.201–.204)                                      │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  zabbix-db     :201 — PostgreSQL                         │  │
│  │  zabbix-server :202 — Zabbix Server                      │  │
│  │  zabbix-front  :203 — Frontend Apache                    │  │
│  │  zabbix-proxy  :204 — Proxy (coleta ansible/docker/mcp)  │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

**Fluxo de dados principal:**

```
Containers Docker (ansible)
    └─► Promtail (via Docker socket) ──► Loki ──► Grafana (LogQL)

Apps K8s (docker)
    └─► OTLP push ──► OTEL Collector ──► Tempo  ──► Grafana (TraceQL)
                                     └─► Loki   ──► Grafana (LogQL)
                                     └─► Prometheus ──► Grafana (PromQL)

kube-state-metrics (docker :32080)
    └─► Prometheus scrape ──► Grafana (dashboard kubernetes-overview)

Zabbix Agent 2 (todas as VMs)
    └─► Zabbix Proxy (.204) ──► Zabbix Server (.202) ──► PostgreSQL (.201)
                                    └─► Zabbix Frontend (.203) ──► Browser
                                    └─► Zabbix MCP (.210) ──► Agente IA
```

---

## 2. Pré-requisitos

### 2.1 Software necessário na máquina local (notebook/workstation)

```bash
# Docker Engine (não Docker Desktop) — versão ≥ 26
docker --version

# Docker Compose v2 (plugin, não o docker-compose standalone)
docker compose version

# make
make --version

# git
git --version

# yamllint (opcional, para o target make lint)
pip install yamllint
```

> **Por que Docker Engine e não Docker Desktop?** Desktop tem overhead e muda o contexto do socket Docker. Em Linux server, o Engine direto é mais simples e o Promtail consegue montar `/var/run/docker.sock` sem restrições adicionais.

### 2.2 Software necessário nas VMs (VM ansible — Fedora 40)

```bash
# Instalar Docker Engine no Fedora 40
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Habilitar e iniciar
sudo systemctl enable --now docker

# Adicionar usuário ao grupo docker (evita sudo em todo comando)
sudo usermod -aG docker $USER
newgrp docker

# make (para os targets do Makefile)
sudo dnf install -y make
```

> **Por que docker group e não sudo?** Adicionar o usuário ao grupo `docker` permite rodar `make up` sem privilégios. O risco é que o grupo docker é equivalente a root — aceitável em lab isolado.

### 2.3 Recursos mínimos por VM

| VM | vCPU | RAM | Disco | Observação |
|----|------|-----|-------|------------|
| ansible | 2 | 4 GB | 32 GB | Stack roda com 2 GB mas fica apertado |
| docker | 4 | 4 GB | 32 GB | Minikube precisa de pelo menos 2 vCPUs |
| mcp-server | 1 | 2 GB | 16 GB | Só roda os binários dos MCP Servers |

---

## 3. Infraestrutura — VMs no Proxmox

### 3.1 Criando as VMs

Todas as VMs do lab usam **Fedora 40** (exceto mcp-server que usa RHEL 9.7). No Proxmox:

1. Baixar a ISO do Fedora Server 40
2. Criar VM com os recursos da tabela acima
3. Instalar com as configurações padrão, criando o usuário `fedora`
4. Configurar IP estático durante a instalação (ou via NetworkManager depois)

**Configurar IP estático via NetworkManager (pós-instalação):**
```bash
# Verificar a interface de rede
ip link show

# Criar conexão estática (substituir ens3 pelo nome real da interface)
sudo nmcli connection modify ens3 \
  ipv4.method manual \
  ipv4.addresses 192.168.10.104/24 \
  ipv4.gateway 192.168.10.1 \
  ipv4.dns "8.8.8.8,8.8.4.4"

sudo nmcli connection up ens3
```

### 3.2 Atenção — CPU type no Proxmox

> **Problema encontrado:** VMs Fedora 40 com CPU type `kvm64` no Proxmox **não executam binários compilados para RHEL 10 (`el10`)**. Isso afeta o Zabbix Agent 2, que tem pacotes separados por versão de RHEL.
>
> **Solução:** sempre usar o repositório `el9` para VMs com Fedora 40 no HomeLAB. Verificar a arquitetura antes de instalar qualquer pacote: `uname -m` (deve retornar `x86_64`).

### 3.3 Firewalld

O Fedora 40 instala o `firewalld` ativo por padrão. Para as VMs de lab em rede privada, a abordagem mais simples é desabilitar ou abrir as portas necessárias:

```bash
# Opção A — desabilitar (lab isolado, sem tráfego externo)
sudo systemctl disable --now firewalld

# Opção B — abrir portas específicas (mais seguro, mas requer manutenção)
sudo firewall-cmd --permanent --add-port=9090/tcp  # Prometheus
sudo firewall-cmd --permanent --add-port=3000/tcp  # Grafana
sudo firewall-cmd --permanent --add-port=3100/tcp  # Loki
sudo firewall-cmd --permanent --add-port=3200/tcp  # Tempo
sudo firewall-cmd --permanent --add-port=4317/tcp  # OTLP gRPC
sudo firewall-cmd --permanent --add-port=4318/tcp  # OTLP HTTP
sudo firewall-cmd --permanent --add-port=9080/tcp  # Promtail
sudo firewall-cmd --permanent --add-port=10050/tcp # Zabbix Agent
sudo firewall-cmd --reload
```

> **Por que mencionar isso aqui?** Metade dos problemas de "serviço não responde" em lab Fedora é o firewalld silenciosamente dropando os pacotes. Verifique sempre com `sudo firewall-cmd --list-all` antes de debugar o serviço em si.

---

## 4. SSH e acesso às VMs

### 4.1 Gerar a chave SSH do lab

```bash
# Na máquina local
ssh-keygen -t ed25519 -f ~/.ssh/homelab_ed25519 -C "homelab-sre"
```

### 4.2 Distribuir a chave para todas as VMs

```bash
# Para cada VM do lab
for ip in 192.168.10.104 192.168.10.112 192.168.10.210; do
  ssh-copy-id -i ~/.ssh/homelab_ed25519.pub fedora@$ip
done
```

### 4.3 Configurar o SSH client (`~/.ssh/config`)

```ssh-config
# HomeLAB — stack de observabilidade
Host 192.168.10.104
  HostName 192.168.10.104
  User fedora
  IdentityFile ~/.ssh/homelab_ed25519

Host 192.168.10.112
  HostName 192.168.10.112
  User fedora
  IdentityFile ~/.ssh/homelab_ed25519

Host 192.168.10.210
  HostName 192.168.10.210
  User fedora
  IdentityFile ~/.ssh/homelab_ed25519
```

Após configurar: `ssh 192.168.10.104` deve entrar sem pedir senha.

---

## 5. Docker e Docker Compose — VM ansible

### 5.1 Clonar o repositório

```bash
# Na VM ansible (192.168.10.104)
ssh 192.168.10.104

# Clonar em /home/fedora/observability
git clone https://github.com/rsantiag085/my-stack-observability.git ~/observability
cd ~/observability
```

> **Por que ~/observability e não /opt ou /srv?** Em lab, `/home/fedora/observability` é mais fácil de operar sem sudo. Em produção, `/opt/observability` com usuário de serviço dedicado seria o padrão.

### 5.2 Copiar e configurar o `.env`

```bash
cp .env.example .env
```

Edite o `.env` com valores reais:

```bash
# Grafana
GF_SECURITY_ADMIN_USER=admin
GF_SECURITY_ADMIN_PASSWORD=minha_senha_segura_minimo_16_chars
GF_SECURITY_SECRET_KEY=string_aleatoria_32_chars_gerada_abaixo

# Gerar o secret_key com:
# python3 -c "import secrets; print(secrets.token_hex(32))"

# Ambiente
LAB_ENV=lab
```

> **Por que `secret_key` importa?** O `GF_SECURITY_SECRET_KEY` é usado pelo Grafana para criptografar credenciais de datasources no banco de dados interno. Se mudar após o Grafana já ter salvo datasources, ele não conseguirá descriptografar e todos os datasources vão "quebrar" (aparecer sem senha). Defina uma vez, guarde com cuidado.

---

## 6. Estrutura de diretórios e variáveis de ambiente

```
observability/
├── docker-compose.yml          # Orquestração dos 6 containers
├── docker-compose.override.yml # Remapeamento alternativo de portas (referência)
├── Makefile                    # Comandos operacionais
├── .env                        # Variáveis reais (no .gitignore)
├── .env.example                # Template variáveis da stack (commitado)
├── .env.zabbix-agent.example   # Template variáveis do agente autônomo (commitado)
│
├── config/
│   ├── prometheus/
│   │   ├── prometheus.yml              # Configuração principal: scrape jobs
│   │   └── rules/
│   │       └── recording.rules.yml     # Recording rules (métricas pré-calculadas)
│   ├── grafana/
│   │   ├── grafana.ini                 # Configurações do servidor Grafana
│   │   ├── provisioning/
│   │   │   ├── datasources/
│   │   │   │   └── datasources.yml     # Prometheus, Loki e Tempo provisionados via código
│   │   │   └── dashboards/
│   │   │       └── dashboards.yml      # Controla como dashboards são carregados
│   │   └── dashboards/                 # JSONs dos dashboards exportados do Grafana
│   ├── loki/
│   │   └── loki.yml                    # Configuração do servidor Loki
│   ├── tempo/
│   │   └── tempo.yml                   # Configuração do servidor Tempo
│   ├── otel/
│   │   └── otel-collector.yml          # Pipeline receivers → processors → exporters
│   └── promtail/
│       └── promtail.yml                # Scrape de logs via Docker socket
│
├── scripts/
│   ├── health-check.sh                 # Valida os 6 endpoints de saúde
│   ├── import-dashboards.sh            # Importa JSONs via API do Grafana (setup fresh)
│   ├── reset-lab.sh                    # Destrói e recria o lab do zero (DESTRUTIVO)
│   ├── agent_orchestrator.py           # Agente SRE v1: webhook → Claude API + MCP (legado)
│   └── zabbix_agent.py                 # Agente SRE v2: webhook → Gemini Flash + MCP Zabbix + SSH
│
└── docs/                               # Documentação
    ├── hosts.md                        # Inventário detalhado das VMs
    ├── setup-guide.md                  # Este arquivo
    ├── runbooks/                       # Procedimentos de troubleshooting
    └── postmortem/                     # Templates e registros de incidentes
```

**Regra de ouro sobre volumes:** todas as configurações são montadas como `ro` (read-only) no container. Isso força o ciclo de trabalho correto: editar o arquivo local → reiniciar o container. Nunca editar dentro do container.

---

## 7. docker-compose.yml — linha a linha

O Compose define 6 serviços em uma rede bridge isolada. Veja o arquivo completo e a explicação de cada decisão:

### 7.1 Rede e volumes

```yaml
networks:
  observability-net:
    driver: bridge
```

Uma rede bridge dedicada faz com que os containers se enxerguem pelo **nome do serviço** como hostname (ex: `http://loki:3100`). Sem isso, seria necessário usar IPs dos containers, que mudam a cada recreação.

```yaml
volumes:
  prometheus_data:
  grafana_data:
  loki_data:
  tempo_data:
```

Volumes nomeados são gerenciados pelo Docker e persistem entre `docker compose down` e `docker compose up`. Somente os dados (não as configs) ficam em volumes — as configs ficam em bind mounts (`./config/...`).

> **Por que não usar bind mounts para dados também?** Bind mounts de dados em Linux criam problemas de permissão (o processo dentro do container roda como UID específico). Volumes nomeados são criados com as permissões corretas automaticamente.

### 7.2 Padrão comum de todos os serviços

Todos os 6 serviços seguem estas convenções:

```yaml
restart: unless-stopped   # reinicia após crash, mas não após docker compose down manual
mem_limit: NNNm           # evita que um container consuma toda a RAM da VM
cpus: "0.N"               # evita starvation de CPU
healthcheck:              # permite que depends_on condition: service_healthy funcione
```

**`restart: unless-stopped`** é a escolha correta para lab: o container reinicia automaticamente após crash (ex: OOM kill) mas para quando você executa `make down` manualmente.

**Limites de recurso:** mesmo em lab, definir limites evita o cenário em que o Loki (por exemplo) engole toda a memória disponível durante ingestão de log burst, derrubando os outros containers.

### 7.3 Sequência de inicialização com `depends_on`

```yaml
grafana:
  depends_on:
    prometheus:
      condition: service_healthy
    loki:
      condition: service_healthy
    tempo:
      condition: service_healthy
```

`condition: service_healthy` significa que o Grafana só inicia depois que Prometheus, Loki **e** Tempo passaram no healthcheck. Isso evita o estado "datasource disponível mas sem dados iniciais" que confunde durante o primeiro boot.

> **Atenção ao healthcheck do Promtail:** a imagem `grafana/promtail:3.0.0` não tem `wget` nem `curl` instalados. O healthcheck padrão com `CMD wget ...` falharia. A solução foi usar o bash nativo:
> ```yaml
> test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/localhost/9080 && echo -e 'GET /ready HTTP/1.0\r\n\r\n' >&3 && grep -q 'Ready' <&3"]
> ```
> Isso abre uma conexão TCP raw via bash e lê a resposta HTTP — sem depender de wget ou curl.

### 7.4 O container Promtail e o Docker socket

```yaml
promtail:
  volumes:
    - ./config/promtail/promtail.yml:/etc/promtail/promtail.yml:ro
    - /var/run/docker.sock:/var/run/docker.sock
```

O mount do Docker socket (`/var/run/docker.sock`) é o que permite ao Promtail descobrir automaticamente todos os containers em execução e coletar seus logs via Docker API. É a diferença entre configurar manualmente cada aplicação para enviar logs vs deixar o Promtail fazer isso automaticamente.

> **Risco de segurança:** montar o Docker socket concede ao container acesso equivalente a root no host. Em produção, use `promtail` como processo no host ou restrinja via `docker-socket-proxy`. Em lab isolado, é aceitável.

---

## 8. Prometheus — coleta e armazenamento de métricas

### 8.1 Flags de inicialização

```yaml
command:
  - --config.file=/etc/prometheus/prometheus.yml
  - --storage.tsdb.path=/prometheus
  - --storage.tsdb.retention.time=15d
  - --web.enable-lifecycle
  - --web.enable-remote-write-receiver
```

| Flag | Por quê |
|------|---------|
| `--storage.tsdb.retention.time=15d` | 15 dias é suficiente para lab; sem esse flag o padrão é 15d mas é bom declarar explicitamente |
| `--web.enable-lifecycle` | Permite recarregar a config sem restart via `POST http://localhost:9090/-/reload` |
| `--web.enable-remote-write-receiver` | Necessário para receber métricas do Tempo (metrics_generator) via remote_write |

> **Atenção em produção:** `--web.enable-lifecycle` e `--web.enable-remote-write-receiver` expõem endpoints de escrita sem autenticação. Proteger com reverse proxy + auth em produção.

### 8.2 `prometheus.yml` — configuração de scrape

```yaml
global:
  scrape_interval: 15s       # a cada 15s coleta métricas de todos os targets
  evaluation_interval: 15s   # a cada 15s avalia as recording rules
  external_labels:
    env: lab                 # adiciona label {env="lab"} em todas as métricas
```

O `external_labels` é importante: quando você tem Prometheus em múltiplos ambientes (lab, staging, prod) e consolida em um único Grafana, esse label permite diferenciar de onde cada métrica veio.

**Scrape jobs:**

```yaml
scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ['localhost:9090']   # o Prometheus monitora a si mesmo
```

Monitorar o próprio Prometheus revela métricas como `prometheus_tsdb_head_samples_appended_total` (taxa de ingestão), `prometheus_target_scrape_pool_targets` (quantos targets) e `up{job="prometheus"}` (se está vivo — redundante mas útil em alertas).

```yaml
  - job_name: kube-state-metrics
    static_configs:
      - targets: ['192.168.10.112:32080']
```

Esse target é **externo à rede Docker** — está na VM docker. Por isso usa IP real ao invés de nome de serviço. A porta `32080` é o port-forward do kubectl que expõe o kube-state-metrics para fora do Minikube (detalhes na seção 15).

### 8.3 Recording rules

```yaml
# config/prometheus/rules/recording.rules.yml
groups:
  - name: lab.recording
    rules:
      - record: job:up:sum
        expr: sum by (job) (up)
```

Recording rules pré-calculam expressões PromQL custosas e as armazenam como novas métricas. O padrão de nome `nivel:metrica:operação` (ex: `job:up:sum`) é convenção oficial do Prometheus.

**Por que isso importa?** Sem recording rules, um dashboard com 20 painéis executando `sum by (job) (rate(...))` faz 20 queries complexas a cada refresh. Com a recording rule, é uma query simples em uma série já calculada.

---

## 9. Loki — agregação de logs

### 9.1 O que é o Loki e por que não o Elasticsearch?

Loki é um sistema de logs **sem índice de conteúdo** — indexa apenas os labels (metadados), não o texto dos logs. Isso resulta em:

- **Custo de storage muito menor** (sem índice invertido)
- **Ingestão mais rápida** (não processa o conteúdo)
- **Query mais lenta** para textos específicos (precisa fazer grep nos chunks)

Para um lab com dezenas de containers, o Loki é ideal. Para um ambiente com bilhões de eventos por dia onde você precisa de full-text search em milissegundos, o Elasticsearch faz mais sentido.

### 9.2 `loki.yml` — configuração

```yaml
auth_enabled: false
```

Desabilita autenticação multi-tenant. Em produção com múltiplos times, cada time teria um `X-Scope-OrgID` e os dados seriam isolados. Em lab, falso simplifica tudo.

```yaml
common:
  instance_addr: 127.0.0.1
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
```

`replication_factor: 1` significa que não há replicação — um único nó. Em produção com múltiplos Loki Ingesters, este valor seria 3 (tolera a falha de um ingestor).

`store: inmemory` para o ring (anel de coordenação) é para modo single-node. Em cluster, usaria `etcd` ou `consul`.

```yaml
schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
```

O schema `v13` com store `tsdb` é o mais eficiente disponível no Loki 3.x. O `from: 2024-01-01` define a data de início do schema — dados anteriores a esta data usariam o schema anterior (se houver). Para novos labs, use uma data no passado recente.

```yaml
limits_config:
  ingestion_rate_mb: 4
  ingestion_burst_size_mb: 8
  max_streams_per_user: 0     # 0 = sem limite
  reject_old_samples: true
  reject_old_samples_max_age: 168h  # 7 dias
```

`reject_old_samples: true` rejeita logs com timestamp mais de 7 dias atrás. Isso evita que uma aplicação mal configurada inunde o Loki com logs históricos e quebre o storage.

---

## 10. Promtail — coleta de logs dos containers

### 10.1 Como o Promtail funciona

O Promtail é o agente de coleta de logs do ecossistema Loki. Ele:
1. Descobre targets (via Docker socket, arquivos, systemd journal)
2. Lê os logs desses targets
3. Aplica transformações (relabeling)
4. Envia para o Loki via HTTP push

### 10.2 `promtail.yml` — configuração

```yaml
clients:
  - url: http://loki:3100/loki/api/v1/push
```

Endpoint de push do Loki. O Promtail é o **único componente que faz push para o Loki** neste lab — todos os outros (OTEL Collector) também enviam ao Loki mas via o mesmo endpoint.

```yaml
scrape_configs:
  - job_name: docker
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
        refresh_interval: 5s
```

`docker_sd_configs` é o service discovery via Docker API. A cada 5 segundos o Promtail pergunta ao Docker quais containers existem e atualiza sua lista de targets. Quando um container novo sobe, começa a coletar logs automaticamente em até 5 segundos.

```yaml
    relabel_configs:
      - source_labels: [__meta_docker_container_name]
        regex: '/(.*)'
        target_label: container
```

O Docker retorna o nome do container com barra inicial (`/prometheus`). O regex `'/(.*)'` captura tudo após a barra, resultando no label `container="prometheus"`. Sem isso, o label seria `container="/prometheus"` — feio para usar em queries.

```yaml
      - source_labels: [__meta_docker_container_label_com_docker_compose_service]
        target_label: service

      - source_labels: [__meta_docker_container_label_com_docker_compose_project]
        target_label: compose_project
```

O Docker Compose adiciona labels automáticos em cada container:
- `com.docker.compose.service` = nome do serviço no Compose (ex: `prometheus`)
- `com.docker.compose.project` = nome do projeto (pasta do docker-compose.yml)

Mapear esses labels permite filtrar no Grafana por serviço Compose: `{service="grafana"}`.

---

## 11. OTEL Collector — receptor central de telemetria

### 11.1 O que é o OTEL Collector e por que usar

O OpenTelemetry Collector é um **proxy de telemetria** — recebe dados de vários formatos, processa e exporta para vários backends. A vantagem:

- As aplicações apontam para **um único endpoint** (o Collector) independente do backend
- O backend pode mudar (ex: trocar Tempo por Jaeger) sem mudar código das apps
- Processamento centralizado: batching, retry, filtros, enriquecimento de dados

### 11.2 `otel-collector.yml` — pipeline

```yaml
extensions:
  health_check:
    endpoint: 0.0.0.0:13133
```

A extensão `health_check` é o que o Docker usa para o healthcheck (`http://localhost:13133/`). Retorna JSON com status.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
```

O Collector aceita dados via dois protocolos OTLP:
- **gRPC (:4317)** — mais eficiente, menor overhead, ideal para alta volumetria
- **HTTP (:4318)** — mais fácil de usar em browsers e apps que não suportam gRPC

```yaml
processors:
  batch:
    timeout: 1s
    send_batch_size: 1024
```

O `batch` processor agrupa telemetria antes de exportar. Sem ele, cada span individual seria enviado separadamente — ineficiente. Com `timeout: 1s`, aguarda até 1 segundo ou 1024 itens para enviar um lote.

```yaml
exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true

  loki:
    endpoint: http://loki:3100/loki/api/v1/push

  prometheusremotewrite:
    endpoint: http://prometheus:9090/api/v1/write
    tls:
      insecure: true
```

Três exporters:
- **Traces → Tempo** via OTLP gRPC
- **Logs → Loki** via HTTP push
- **Métricas → Prometheus** via remote_write (por isso o Prometheus precisa de `--web.enable-remote-write-receiver`)

```yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/tempo]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [loki]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheusremotewrite]
```

Três pipelines independentes: traces, logs e métricas. Cada um tem seu próprio caminho receiver → processor → exporter. Se um pipeline falhar, os outros continuam funcionando.

---

## 12. Tempo — traces distribuídos

### 12.1 O que são traces distribuídos

Um trace registra o caminho de uma requisição através de múltiplos serviços. Exemplo: uma requisição HTTP que chama 3 microserviços gera 1 trace com 3+ spans. O Tempo armazena esses traces e permite consultas como "mostre todos os traces lentos do serviço X nas últimas 2 horas".

### 12.2 `tempo.yml` — configuração

```yaml
distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317
        http:
          endpoint: 0.0.0.0:4318
```

O Tempo também aceita OTLP diretamente. No lab, os traces chegam via OTEL Collector (que os repassa ao Tempo), mas apps poderiam enviar diretamente ao Tempo na porta 4317 também.

```yaml
compactor:
  compaction:
    block_retention: 48h
```

Retenção de 48 horas para traces. Em produção, 7–30 dias é comum. Para lab, 48h é suficiente — traces de experimentos recentes ficam disponíveis mas sem consumir muito disco.

```yaml
metrics_generator:
  registry:
    external_labels:
      source: tempo
      env: lab
  storage:
    path: /tmp/tempo/generator/wal
    remote_write:
      - url: http://prometheus:9090/api/v1/write
        send_exemplars: true
```

O `metrics_generator` é uma feature do Tempo que **gera métricas automaticamente a partir dos traces**. Ele cria:
- `service_graph_request_total` — contador de chamadas entre serviços
- `traces_spanmetrics_duration_milliseconds_bucket` — latência por operação

Essas métricas são enviadas ao Prometheus via remote_write, permitindo montar o **Service Map** no Grafana (visualização de topologia de serviços derivada dos traces).

```yaml
overrides:
  defaults:
    metrics_generator:
      processors: [service-graphs, span-metrics]
```

Dois processadores ativados:
- `service-graphs` — gera o grafo de dependências entre serviços
- `span-metrics` — gera histogramas de latência por operação/serviço

---

## 13. Grafana 13 — visualização e dashboards

### 13.1 `grafana.ini` — configuração do servidor

```ini
[server]
domain = 192.168.10.104
root_url = http://192.168.10.104:3000/
enable_gzip = true
```

`root_url` é importante para links compartilhados e notificações de alerta — define a URL base que o Grafana usa para gerar links absolutos.

```ini
[security]
disable_gravatar = true
cookie_secure = false
```

`disable_gravatar = true` — sem TLS no lab, a imagem de avatar seria carregada via HTTP externo. Desabilitar evita requisições externas desnecessárias.

`cookie_secure = false` — o cookie `secure` só funciona com HTTPS. Em HTTP do lab, precisa ser `false` ou o login não funciona.

```ini
[plugins]
allow_loading_unsigned_plugins = jdbranham-diagram-panel
```

O plugin `jdbranham-diagram-panel` (GraphViz/Mermaid) não é assinado oficialmente pela Grafana. Essa linha permite carregá-lo mesmo sem assinatura. Por segurança, lista-se apenas os plugins específicos, não `allow_loading_unsigned_plugins = *`.

```ini
[analytics]
reporting_enabled = false
check_for_updates = false
```

Sem telemetria e sem verificação de atualizações — o lab está em rede privada sem acesso à internet (ou com acesso limitado).

### 13.2 Credenciais via variáveis de ambiente

```yaml
# docker-compose.yml
environment:
  - GF_SECURITY_ADMIN_USER=${GF_SECURITY_ADMIN_USER}
  - GF_SECURITY_ADMIN_PASSWORD=${GF_SECURITY_ADMIN_PASSWORD}
  - GF_SECURITY_SECRET_KEY=${GF_SECURITY_SECRET_KEY}
  - GF_INSTALL_PLUGINS=jdbranham-diagram-panel
```

`GF_INSTALL_PLUGINS` instrui o Grafana a baixar e instalar o plugin na inicialização. A versão não é fixada aqui — para fixar, use `GF_INSTALL_PLUGINS=jdbranham-diagram-panel 1.10.4`.

> **Sobre o plugin jdbranham-diagram-panel:** apesar do nome "GraphViz", o plugin usa **sintaxe Mermaid.js** para renderizar diagramas, não a linguagem DOT do GraphViz clássico. Detalhes em `graphviz_guide.md`.

### 13.3 Provisioning de datasources

O arquivo `config/grafana/provisioning/datasources/datasources.yml` é montado em `/etc/grafana/provisioning/datasources/`. O Grafana lê este diretório na inicialização e cria os datasources automaticamente.

```yaml
datasources:
  - name: Prometheus
    type: prometheus
    uid: PBFA97CFB590B2093    # UID fixo e permanente
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    jsonData:
      timeInterval: 15s        # deve corresponder ao scrape_interval do Prometheus
      httpMethod: POST
```

O `uid` fixo é crítico: os dashboards JSONs referem-se aos datasources por UID. Se o UID mudar, todos os painéis quebram com "datasource not found". Ao provisionar via código, o UID é estável entre recriações do ambiente.

```yaml
  - name: Loki
    uid: P8E80F9AEF21F6940
    url: http://loki:3100
    jsonData:
      derivedFields:
        - datasourceUid: tempo
          matcherRegex: '"traceID":"(\w+)"'
          name: TraceID
          url: '$${__value.raw}'
```

`derivedFields` configura correlação logs → traces: quando um log contém `"traceID":"abc123"`, o Grafana exibe um link clicável que abre o trace correspondente no Tempo. Para funcionar, os logs das aplicações precisam incluir o `traceID` no formato JSON.

```yaml
  - name: Tempo
    uid: tempo
    jsonData:
      serviceMap:
        datasourceUid: prometheus   # gráfico de serviços vem do Prometheus
      lokiSearch:
        datasourceUid: loki         # busca de logs correlacionados
      tracesToLogsV2:
        datasourceUid: loki
        spanStartTimeShift: '-1h'
        spanEndTimeShift: '1h'
        filterByTraceID: true
```

Essas configurações ativam o **Tempo Explore**: ao abrir um trace, o Grafana automaticamente:
1. Busca logs do Loki no intervalo `[span_start - 1h, span_end + 1h]` com o mesmo `traceID`
2. Exibe o Service Map (grafo de serviços) vindo das métricas do Prometheus

### 13.4 Provisioning de dashboards — problema conhecido no Grafana 13

O file provisioning de dashboards (`allowUiUpdates: true`) **não funciona** no Grafana 13 com o novo Unified Storage. Se você provisionar um dashboard via arquivo JSON e tentar editá-lo no Grafana, ele diz "dashboard is managed by config" e bloqueia edições.

**Solução adotada:** provisioning via arquivo foi desabilitado. O workflow é:

```bash
# 1. Criar/editar o dashboard no Grafana via UI
# 2. Exportar via API
curl -s -u admin:senha http://192.168.10.104:3000/api/dashboards/uid/SEU_UID \
  | python3 -m json.tool > config/grafana/dashboards/nome-dashboard.json

# 3. Commitar o JSON
git add config/grafana/dashboards/nome-dashboard.json
git commit -m "dashboard: atualiza kubernetes-overview"

# 4. Em setup fresh, importar via script
./scripts/import-dashboards.sh
```

---

## 14. Zabbix — monitoramento de infraestrutura

### 14.1 Arquitetura Zabbix do lab

O Zabbix já existia antes da stack LGTM. A arquitetura é em 3 camadas:

```
Zabbix Agent 2 (em cada VM)
        │ porta 10050
        ▼
Zabbix Proxy (192.168.10.204) ← coleta: ansible, docker, mcp-server
        │
        ▼ porta 10051
Zabbix Server (192.168.10.202)
        │
        ├──► PostgreSQL (192.168.10.201) — armazena dados
        └──► Frontend (192.168.10.203) — visualização web
```

**Por que usar Proxy?** O Zabbix Proxy reduz a carga no Server e permite coletar dados de hosts em segmentos de rede diferentes. No lab, o proxy foi configurado para coletar as VMs que foram adicionadas posteriormente (ansible, docker, mcp-server).

### 14.2 Instalação do Zabbix Agent 2 nas VMs

```bash
# Em cada VM (Fedora 40) — usar repositório el9 (não el10, ver seção 3.2)
sudo rpm -Uvh https://repo.zabbix.com/zabbix/7.0/rhel/9/x86_64/zabbix-release-latest-7.0.el9.noarch.rpm
sudo dnf clean all
sudo dnf install -y zabbix-agent2

# Configurar o agente
sudo tee /etc/zabbix/zabbix_agent2.conf > /dev/null <<'EOF'
Server=192.168.10.204          # Proxy (para ansible, docker, mcp-server)
ServerActive=192.168.10.204    # Proxy
Hostname=ansible               # Nome do host como cadastrado no Zabbix
EOF

# Habilitar e iniciar
sudo systemctl enable --now zabbix-agent2

# Abrir porta no firewalld (se ativo)
sudo firewall-cmd --permanent --add-port=10050/tcp
sudo firewall-cmd --reload
```

> **Atenção ao `Hostname`:** o valor em `Hostname=` deve ser **idêntico** ao nome cadastrado no Zabbix Frontend. Maiúsculas e minúsculas importam. Se o host no Zabbix se chama `ansible` e o agente diz `Ansible`, o Server não vai reconhecer.

### 14.3 Adicionando hosts no Zabbix Frontend

1. Acesse `http://192.168.10.203/zabbix/` → **Configuration → Hosts → Create host**
2. Preencha:
   - **Host name:** `ansible` (exato, igual ao `Hostname` no agente)
   - **Groups:** Linux servers
   - **Interfaces:** Agent, IP `192.168.10.104`, Port `10050`
   - **Monitored by:** Proxy `zabbix-proxy`
3. Na aba **Templates:** adicionar `Linux by Zabbix agent`
4. **Update**

Aguarde 1–2 minutos. O ícone do host ficará verde quando o agente responder.

### 14.4 Verificando a disponibilidade do agente

```bash
# No Zabbix Server, testar conexão ao agente via proxy
zabbix_get -s 192.168.10.104 -p 10050 -k system.hostname

# Se o proxy estiver no meio, testar direto:
ssh 192.168.10.204
zabbix_get -s 192.168.10.104 -p 10050 -k system.hostname
```

---

## 15. Kubernetes — Minikube na VM docker

### 15.1 Instalação do Minikube

```bash
# Na VM docker (192.168.10.112)
ssh 192.168.10.112

# Instalar Docker (pré-requisito do Minikube com driver docker)
sudo dnf install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
sudo usermod -aG docker fedora
newgrp docker

# Instalar kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# Instalar Minikube
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install minikube-linux-amd64 /usr/local/bin/minikube

# Iniciar o cluster (driver docker, 4 vCPUs, 3 GB RAM)
minikube start --driver=docker --cpus=4 --memory=3072

# Verificar
kubectl get nodes
# NAME       STATUS   ROLES           AGE   VERSION
# minikube   Ready    control-plane   1m    v1.30.0
```

### 15.2 Estrutura de manifestos

Os manifestos ficam em `/home/fedora/k8s-obs/` na VM docker, organizados por aplicação:

```
k8s-obs/
├── namespace/
├── painel-estudos-sre/
│   ├── deployment/painel-estudos-sre-deploy.yml
│   ├── service/painel-estudos-sre-service.yml
│   └── pvc/sqlite-pvc.yml
└── zabbix-monitoring/
```

### 15.3 Namespace

```bash
# Criar namespace para as aplicações
kubectl create namespace apps
```

### 15.4 Manifestos do `painel-estudos-sre`

**PersistentVolumeClaim** (`sqlite-pvc.yml`) — armazenamento para o banco SQLite:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: painel-estudos-sre-pvc
  namespace: apps
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
  storageClassName: standard
```

`ReadWriteOnce` significa que o volume pode ser montado por **um único pod** de cada vez. Para SQLite (que não suporta acesso concorrente via rede), é o modo correto.

**Deployment** (`painel-estudos-sre-deploy.yml`):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: painel-estudos-sre-deploy
  namespace: apps
spec:
  replicas: 1
  selector:
    matchLabels:
      app: painel-estudos-sre
  template:
    metadata:
      labels:
        app: painel-estudos-sre
    spec:
      containers:
      - name: painel-estudos-sre
        image: rsantiag085/painel-estudos-sre:v1.2
        ports:
        - containerPort: 8000
        resources:
          requests:
            memory: "128Mi"
            cpu: "100m"
          limits:
            memory: "256Mi"
            cpu: "250m"
        readinessProbe:
          httpGet:
            path: /
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
        livenessProbe:
          httpGet:
            path: /
            port: 8000
          initialDelaySeconds: 15
          periodSeconds: 30
        volumeMounts:
        - name: sqlite-data
          mountPath: /data
      volumes:
      - name: sqlite-data
        persistentVolumeClaim:
          claimName: painel-estudos-sre-pvc
```

**`readinessProbe` vs `livenessProbe`:**
- `readinessProbe` — controla se o pod recebe tráfego. Se falhar, o pod sai do pool do Service mas **não reinicia**. Use para aguardar warmup.
- `livenessProbe` — controla se o pod está vivo. Se falhar, o Kubernetes **reinicia** o pod. Use para detectar deadlocks.

**Service** (`painel-estudos-sre-service.yml`):

```yaml
apiVersion: v1
kind: Service
metadata:
  name: painel-estudos-sre-service
  namespace: apps
spec:
  selector:
    app: painel-estudos-sre
  ports:
    - protocol: TCP
      port: 80
      targetPort: 8000
  type: NodePort
```

`NodePort` expõe o serviço em uma porta aleatória do Node (entre 30000–32767). Em produção usaríamos `LoadBalancer` ou `Ingress`. No Minikube, `NodePort` é o caminho mais simples para acessar apps externamente.

**Aplicando os manifestos:**

```bash
kubectl apply -f /home/fedora/k8s-obs/painel-estudos-sre/pvc/sqlite-pvc.yml
kubectl apply -f /home/fedora/k8s-obs/painel-estudos-sre/deployment/painel-estudos-sre-deploy.yml
kubectl apply -f /home/fedora/k8s-obs/painel-estudos-sre/service/painel-estudos-sre-service.yml

# Verificar
kubectl get pods -n apps
kubectl get svc -n apps
```

### 15.5 kube-state-metrics — métricas do cluster para o Prometheus

O `kube-state-metrics` exporta o estado dos objetos Kubernetes (Pods, Deployments, Nodes) como métricas Prometheus.

```bash
# Instalar via Helm
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kube-state-metrics prometheus-community/kube-state-metrics \
  --version 2.19.0 \
  --namespace kube-system \
  --set image.tag=v2.12.0

# Verificar
kubectl get pods -n kube-system | grep kube-state
```

### 15.6 Port-forwards persistentes via systemd

Por padrão, o `kubectl port-forward` morre quando a sessão SSH encerra. A solução é criar serviços systemd para torná-los persistentes:

**`/etc/systemd/system/minikube-start.service`** — inicia o Minikube no boot:

```ini
[Unit]
Description=Minikube — inicialização do cluster local
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=fedora
Environment=HOME=/home/fedora
ExecStart=/usr/local/bin/minikube start
ExecStop=/usr/local/bin/minikube stop
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/painel-estudos-sre-portforward.service`** — port-forward da app:

```ini
[Unit]
Description=Port-forward — painel-estudos-sre (8080 → pod:80)
After=minikube-start.service
Requires=minikube-start.service

[Service]
Type=simple
User=fedora
Environment=HOME=/home/fedora
ExecStartPre=/usr/local/bin/kubectl wait --for=condition=Ready pod \
  -n apps -l app=painel-estudos-sre --timeout=120s
ExecStart=/usr/local/bin/kubectl port-forward \
  -n apps svc/painel-estudos-sre-service 8080:80 --address 0.0.0.0
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

> **Por que `ExecStartPre` com `kubectl wait`?** O `port-forward` falha se o pod ainda não estiver Ready. O `kubectl wait` pausa a inicialização do serviço systemd até o pod estar pronto (ou timeout de 120s). Sem isso, uma corrida de inicialização faria o serviço falhar e reiniciar em loop.

**`/etc/systemd/system/kube-state-metrics-portforward.service`** — port-forward do kube-state-metrics:

```ini
[Unit]
Description=Port-forward — kube-state-metrics (32080 → pod:8080)
After=minikube-start.service
Requires=minikube-start.service

[Service]
Type=simple
User=fedora
Environment=HOME=/home/fedora
ExecStartPre=/usr/local/bin/kubectl wait --for=condition=Ready pod \
  -n kube-system -l app.kubernetes.io/name=kube-state-metrics --timeout=120s
ExecStart=/usr/local/bin/kubectl port-forward \
  -n kube-system svc/kube-state-metrics 32080:8080 --address 0.0.0.0
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Habilitar todos os serviços:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now minikube-start
sudo systemctl enable --now painel-estudos-sre-portforward
sudo systemctl enable --now kube-state-metrics-portforward
```

### 15.7 DNAT para o apiserver do Minikube

O Kubernetes MCP Server (seção 16) precisa acessar o apiserver do Minikube. O apiserver escuta em `192.168.49.2:8443` — IP interno da rede Docker do Minikube, não acessível de outras VMs.

A solução é criar uma regra DNAT (Destination NAT) no iptables da VM docker:

**`/etc/systemd/system/minikube-apiserver-dnat.service`:**

```ini
[Unit]
Description=DNAT — Expõe apiserver do Minikube em 192.168.10.112:8443
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c '\
  iptables -t nat -C PREROUTING -p tcp --dport 8443 -j DNAT --to-destination 192.168.49.2:8443 2>/dev/null || \
  iptables -t nat -A PREROUTING -p tcp --dport 8443 -j DNAT --to-destination 192.168.49.2:8443; \
  iptables -C FORWARD -p tcp -d 192.168.49.2 --dport 8443 -j ACCEPT 2>/dev/null || \
  iptables -A FORWARD -p tcp -d 192.168.49.2 --dport 8443 -j ACCEPT; \
  sysctl -w net.ipv4.ip_forward=1'
ExecStop=/bin/bash -c '\
  iptables -t nat -D PREROUTING -p tcp --dport 8443 -j DNAT --to-destination 192.168.49.2:8443 2>/dev/null; \
  iptables -D FORWARD -p tcp -d 192.168.49.2 --dport 8443 -j ACCEPT 2>/dev/null; \
  true'

[Install]
WantedBy=multi-user.target
```

> **Por que o check `-C` antes do `-A`?** O `ExecStart` pode ser executado mais de uma vez (por exemplo, após `systemctl restart`). Sem o check, adicionaria regras duplicadas no iptables, o que não quebra nada mas polui a tabela. O `-C` verifica se a regra já existe antes de adicionar.

---

## 16. MCP Servers — Zabbix e Kubernetes para IA

### 16.1 O que é o MCP (Model Context Protocol)

MCP é um protocolo aberto que padroniza como agentes de IA se conectam a ferramentas e fontes de dados. Em vez de cada agente implementar sua própria integração com o Zabbix ou o Kubernetes, um MCP Server expõe essas ferramentas em um formato padrão que qualquer cliente MCP (Claude, OpenAI, etc.) pode usar.

No lab, temos dois MCP Servers na VM `mcp-server` (192.168.10.210):

| MCP Server | Porta | Ferramenta | Modo |
|------------|-------|------------|------|
| `initMAX/zabbix-mcp-server` | 8080 | API do Zabbix (237 ferramentas) | read/write |
| `containers/kubernetes-mcp-server` | 8081 | Minikube via kubeconfig | read-only |

O agente autônomo (`zabbix_agent.py` v2.0.0) usa apenas o Zabbix MCP Server. Das 237 ferramentas disponíveis, ele expõe ao Gemini um subset de 14 ferramentas relevantes para SRE:

| Ferramenta (MCP) | Finalidade |
|------------------|------------|
| `host_get` | Dados cadastrais e IP dos hosts |
| `host_status_get` | Status resumido dos hosts |
| `problem_get` | Problemas registrados |
| `problem_active_get` | Problemas ativos no momento |
| `event_get` | Eventos recentes |
| `event_acknowledge` | Acknowledge com comentário e action flag |
| `item_get` | Itens de coleta de um host |
| `item_history_summary_get` | Resumo estatístico do histórico de um item |
| `history_get` | Valores históricos de um item |
| `trigger_get` | Estado das triggers |
| `alert_get` | Alertas disparados |
| `infrastructure_summary_get` | Saúde geral da infraestrutura |
| `script_get` | Scripts disponíveis no Zabbix |
| `script_execute` | Executar script Zabbix no host (⚠️ condicional) |

Além do MCP, o agente possui a ferramenta customizada `ssh_execute` — implementada diretamente no código — para reiniciar o `zabbix-agent2` em hosts com o agente Zabbix indisponível (cenário onde o MCP não consegue coletar dados do host afetado).

### 16.2 Zabbix MCP Server

**Instalação:**

```bash
# Na VM mcp-server (192.168.10.210)
ssh 192.168.10.210

# Baixar e instalar (ver release mais recente em github.com/initMAX/zabbix-mcp-server)
# O binário é distribuído como RPM para RHEL 9
sudo rpm -i zabbix-mcp-server-1.30-1.el9.x86_64.rpm

# Configurar o arquivo .env com as credenciais do Zabbix
sudo tee /etc/zabbix-mcp/.env > /dev/null <<'EOF'
ZABBIX_URL=http://192.168.10.203/zabbix
ZABBIX_USER=Admin
ZABBIX_PASSWORD=CHANGE_ME
EOF
sudo chmod 600 /etc/zabbix-mcp/.env

# Habilitar e iniciar
sudo systemctl enable --now zabbix-mcp-server

# Verificar
curl -s http://192.168.10.210:8080/health
```

### 16.3 Kubernetes MCP Server

> **Nota (v2.0.0):** o `zabbix_agent.py` não usa o Kubernetes MCP Server — a integração K8s está prevista para uma versão futura. O servidor continua instalado e acessível para uso via Claude Code ou outros clientes MCP.

Este é mais trabalhoso porque requer preparar o kubeconfig do Minikube (ver detalhes completos em `docs/runbooks/RB-004-instalacao-kubernetes-mcp-server.md`). O resumo:

1. Copiar o kubeconfig do Minikube (da VM docker)
2. Substituir o IP interno (`192.168.49.2`) pelo IP externo da VM docker (`192.168.10.112`)
3. Embutir os certificados em base64 (o kubeconfig original referencia arquivos locais)
4. Criar regra DNAT na VM docker para o apiserver (seção 15.7)
5. Instalar o binário e criar o serviço systemd

**Configuração TOML** (`/etc/kubernetes-mcp/config.toml`):

```toml
log_level = 2
read_only = true          # apenas leitura — agente não pode modificar o cluster
toolsets = ["core", "config"]

# Bloquear recursos sensíveis
[[denied_resources]]
group = ""
version = "v1"
kind = "Secret"

[[denied_resources]]
group = ""
version = "v1"
kind = "ServiceAccount"
```

**`/etc/systemd/system/kubernetes-mcp-server.service`:**

```ini
[Unit]
Description=Kubernetes MCP Server — HomeLAB (Minikube)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nobody
Group=nobody
ExecStart=/opt/kubernetes-mcp-server/kubernetes-mcp-server \
  --config /etc/kubernetes-mcp/config.toml \
  --kubeconfig /etc/kubernetes-mcp/kubeconfig \
  --port 8081 \
  --log-level 2
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/etc/kubernetes-mcp

[Install]
WantedBy=multi-user.target
```

> **Por que rodar como `nobody`?** Princípio do menor privilégio: o MCP Server só lê dados via kubeconfig, não precisa de nenhum privilégio de sistema. `nobody` não tem home directory nem shell, minimizando a superfície de ataque se o processo for comprometido.

---

## 17. Agente Autônomo de Incidentes — `zabbix_agent.py`

### 17.1 O que é e o que faz

O `scripts/zabbix_agent.py` é um agente de resposta a incidentes que recebe webhooks do Zabbix, investiga o problema de forma autônoma usando o Gemini Flash 2.5 com as ferramentas do MCP Zabbix, e toma ações corretivas sem intervenção humana para um conjunto de padrões conhecidos.

**Fluxo completo:**

```
Trigger Zabbix → Action → Media Type "Zabbix Agent Autônomo"
  → POST http://192.168.10.108:9001 (workstation)
  → aguarda 60s (descarta silenciosamente se resolver sozinho)
  → event_acknowledge no Zabbix ("🤖 Agente autônomo assumiu")
  → Gemini Flash: host_get → ssh_execute restart → problem_active_get
  → Telegram: resumo do diagnóstico e ação tomada
```

**Ações autônomas (sem aprovação humana):**

| Padrão detectado | Ação tomada |
|------------------|-------------|
| `agent is not available` | `ssh_execute: sudo systemctl restart zabbix-agent2` no host afetado |
| Qualquer incidente persistente > 60s | Acknowledge no Zabbix com comentário do agente |
| Qualquer incidente | Notificação Telegram com diagnóstico e resultado |

### 17.2 Pré-requisitos

**Python e dependências:**

```bash
# Na workstation (192.168.10.108) — onde o agente roda
python3 --version   # ≥ 3.10

pip install mcp google-generativeai httpx python-dotenv
```

**Chave SSH:** o agente usa a mesma chave `homelab_ed25519` da seção 4, mas conecta como o usuário `svc-zabbix` (conta de serviço configurada na seção 17.3).

### 17.3 Conta de serviço `svc-zabbix`

O agente executa comandos privilegiados via SSH usando uma conta de serviço dedicada com sudo restrito. A conta foi criada nas 6 VMs do inventário (104, 112, 210, 202, 203, 204):

```bash
# Em CADA VM do inventário:
ssh <ip-da-vm>

# 1. Criar o usuário
sudo useradd -m -s /bin/bash svc-zabbix

# 2. Autorizar a chave SSH da workstation
sudo mkdir -p /home/svc-zabbix/.ssh
# Copiar o conteúdo de ~/.ssh/homelab_ed25519.pub da workstation:
sudo bash -c 'echo "ssh-ed25519 AAAA... homelab-sre" >> /home/svc-zabbix/.ssh/authorized_keys'
sudo chmod 700 /home/svc-zabbix/.ssh
sudo chmod 600 /home/svc-zabbix/.ssh/authorized_keys
sudo chown -R svc-zabbix:svc-zabbix /home/svc-zabbix/.ssh

# 3. Sudo NOPASSWD restrito ao allowlist do agente
sudo tee /etc/sudoers.d/svc-zabbix > /dev/null <<'EOF'
svc-zabbix ALL=(ALL) NOPASSWD: \
  /bin/systemctl restart zabbix-agent2, \
  /bin/systemctl start zabbix-agent2, \
  /bin/systemctl stop zabbix-agent2, \
  /bin/systemctl status zabbix-agent2, \
  /bin/systemctl restart zabbix-agent, \
  /bin/systemctl start zabbix-agent, \
  /bin/systemctl stop zabbix-agent, \
  /bin/systemctl status zabbix-agent
EOF
sudo chmod 440 /etc/sudoers.d/svc-zabbix
```

**Verificar:**

```bash
# Da workstation — deve retornar o status sem pedir senha
ssh -i ~/.ssh/homelab_ed25519 svc-zabbix@192.168.10.210 "sudo systemctl status zabbix-agent2"
```

### 17.4 Variáveis de ambiente (`.env.zabbix-agent`)

```bash
# Na workstation — criar a partir do template
cd ~/projetos/my-stack-observability
cp .env.zabbix-agent.example .env.zabbix-agent
```

Edite o `.env.zabbix-agent` com os valores reais:

```bash
# Google Gemini API (aistudio.google.com → Get API key)
# Tier gratuito: 1.500 requisições/dia — suficiente para lab
GEMINI_API_KEY=sua_chave_aqui
GEMINI_MODEL=gemini-2.5-flash

# MCP Server do Zabbix
ZABBIX_MCP_URL=http://192.168.10.210:8080/mcp
ZABBIX_MCP_TOKEN=    # deixar vazio se o MCP não exige autenticação

# Notificação Telegram
TELEGRAM_BOT_TOKEN=seu_token_do_bot
TELEGRAM_CHAT_ID=seu_chat_id

# Servidor webhook (porta 9001 — não conflita com agent_orchestrator v1 na 9000)
WEBHOOK_PORT=9001
WEBHOOK_SECRET=string_secreta_minimo_32_chars

# SSH — conta de serviço criada na seção 17.3
SSH_USER=svc-zabbix
SSH_KEY_PATH=/home/<seu-usuario>/.ssh/homelab_ed25519
```

### 17.5 Subindo o agente

```bash
cd ~/projetos/my-stack-observability

# O diretório de logs é necessário para o FileHandler do agente
mkdir -p logs

# Subir em background
nohup python scripts/zabbix_agent.py --mode server --port 9001 \
  >> logs/zabbix-agent.log 2>&1 &

# Verificar que está rodando
ps aux | grep zabbix_agent | grep -v grep
tail -5 logs/zabbix-agent.log
# Esperado: "Zabbix Agent iniciado — escutando em 0.0.0.0:9001"
#           "Modelo: gemini-2.5-flash | MCP: http://192.168.10.210:8080/mcp"
```

### 17.6 Configurar Media Type no Zabbix

1. Acesse `http://192.168.10.203/zabbix/` → **Administration → Media types → Create media type**
2. Preencha:
   - **Name:** `Zabbix Agent Autônomo`
   - **Type:** Webhook
   - **Script:**
     ```javascript
     var params = JSON.parse(value);
     var req = new HttpRequest();
     req.addHeader('Content-Type: application/json');
     if (params.webhook_secret) {
         req.addHeader('X-Webhook-Secret: ' + params.webhook_secret);
     }
     var resp = req.post(
         'http://192.168.10.108:9001',
         JSON.stringify({
             source:    'zabbix',
             host:      params.host,
             ip:        params.host_ip,
             problem:   params.problem,
             severity:  params.severity,
             trigger:   params.trigger_name,
             eventid:   params.eventid,
             timestamp: params.timestamp,
             extra: {
                 trigger_id: params.triggerid,
                 event_id:   params.eventid
             }
         })
     );
     return resp;
     ```
   - **Parameters** (criar cada um):

     | Nome | Valor (macro Zabbix) |
     |------|----------------------|
     | `host` | `{HOST.NAME}` |
     | `host_ip` | `{HOST.IP}` |
     | `problem` | `{TRIGGER.NAME}` |
     | `severity` | `{TRIGGER.SEVERITY}` |
     | `trigger_name` | `{TRIGGER.NAME}` |
     | `triggerid` | `{TRIGGER.ID}` |
     | `eventid` | `{EVENT.ID}` |
     | `timestamp` | `{EVENT.DATE} {EVENT.TIME}` |
     | `webhook_secret` | `<valor do WEBHOOK_SECRET>` |

3. **Update** e **Test** — o teste deve retornar status 200.

### 17.7 Configurar Action no Zabbix

1. **Configuration → Actions → Trigger actions → Create action**
2. Aba **Action:**
   - **Name:** `Agente Autônomo — Incidentes`
   - **Conditions:** trigger severity ≥ `Warning` (ou conforme preferência)
3. Aba **Operations:**
   - Adicionar operação: **Send to users** → usuário Admin → **Send only to** → `Zabbix Agent Autônomo`
4. **Update**

### 17.8 Testar

**Modo test local (sem Zabbix):**

```bash
# Simula um incidente de container caído — não aguarda os 60s de persistência
python scripts/zabbix_agent.py --mode test
tail -f logs/zabbix-agent.log
```

**Teste de ponta a ponta:**

```bash
# 1. Confirmar agente rodando
ps aux | grep zabbix_agent | grep -v grep

# 2. Parar o zabbix-agent2 no mcp-server para disparar a trigger
ssh svc-zabbix@192.168.10.210 "sudo systemctl stop zabbix-agent2"

# 3. Aguardar 1–2 min para o Zabbix detectar e disparar o webhook

# 4. Acompanhar em tempo real
tail -f logs/zabbix-agent.log
```

**O que esperar no log:**

```
~t+0s   INFO  HTTP <ip> POST / ...accepted
~t+0s   INFO  Aguardando 60s para confirmar persistência — eventid <id>
~t+60s  INFO  Acknowledge enviado após 60s — eventid <id>
~t+60s  INFO  Ferramentas MCP: 237 disponíveis → 14 + ssh_execute expostas ao Gemini
~t+62s  INFO  [TURN 1] → host_get(...)
~t+64s  INFO  [TURN 2] → ssh_execute({'host_ip': '192.168.10.210', 'command': 'sudo systemctl restart zabbix-agent2'})
~t+65s  INFO  SSH → svc-zabbix@192.168.10.210: sudo systemctl restart zabbix-agent2
~t+66s  INFO  [TURN 3] → problem_active_get(...)
~t+68s  INFO  Agente concluído em 8.2s | resolvido=True | escalado=False
~t+68s  INFO  Notificação enviada via Telegram
```

---

## 18. Subindo tudo e validando

### 18.1 Ordem de inicialização

```bash
# 1. VM docker — Minikube e port-forwards
ssh 192.168.10.112
sudo systemctl status minikube-start painel-estudos-sre-portforward kube-state-metrics-portforward minikube-apiserver-dnat

# 2. VM mcp-server — MCP Servers
ssh 192.168.10.210
sudo systemctl status zabbix-mcp-server kubernetes-mcp-server

# 3. VM ansible — stack de observabilidade
ssh 192.168.10.104
cd ~/observability
make up

# 4. Workstation — agente autônomo de incidentes
cd ~/projetos/my-stack-observability
mkdir -p logs
nohup python scripts/zabbix_agent.py --mode server --port 9001 \
  >> logs/zabbix-agent.log 2>&1 &
ps aux | grep zabbix_agent | grep -v grep   # confirmar que subiu
```

### 18.2 Validar a stack de observabilidade

```bash
# Health check de todos os 6 serviços
make health

# Ou verificar cada endpoint manualmente
curl http://192.168.10.104:9090/-/healthy      # Prometheus
curl http://192.168.10.104:3000/api/health     # Grafana
curl http://192.168.10.104:3100/ready          # Loki
curl http://192.168.10.104:3200/ready          # Tempo
curl http://192.168.10.104:13133/              # OTEL Collector
curl http://192.168.10.104:9080/ready          # Promtail
```

### 18.3 Validar os targets do Prometheus

```bash
# Todos os targets devem aparecer como "UP"
curl -s http://192.168.10.104:9090/api/v1/targets \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for t in data['data']['activeTargets']:
    print(t['health'].upper(), t['labels']['job'], t['labels']['instance'])
"
```

Esperado:
```
UP prometheus  localhost:9090
UP grafana     grafana:3000
UP loki        loki:3100
UP tempo       tempo:3200
UP kube-state-metrics  192.168.10.112:32080
```

### 18.4 Validar os MCP Servers

```bash
# Zabbix MCP
curl -s http://192.168.10.210:8080/health
# Esperado: {"status":"ok"}

# Kubernetes MCP
curl -s http://192.168.10.210:8081/healthz
# Esperado: 200 OK
```

### 18.5 Importar os dashboards no Grafana (setup fresh)

```bash
cd ~/observability
./scripts/import-dashboards.sh
```

O script importa os três dashboards JSON de `config/grafana/dashboards/` via API do Grafana.

### 18.6 Verificar Zabbix

```bash
# No Zabbix Frontend: http://192.168.10.203/zabbix/
# Monitoring → Hosts → todos devem ter ícone verde (agent available)

# Via Zabbix API
curl -s -X POST http://192.168.10.203/zabbix/api_jsonrpc.php \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"host.get","params":{"output":["name","available"]},"auth":"SEU_TOKEN","id":1}' \
  | python3 -m json.tool
```

---

## 19. Problemas conhecidos e decisões de design

### 19.1 Grafana `unhealthy` no boot

**Sintoma:** `docker ps` mostra `grafana Up 2 minutes (unhealthy)` logo após o boot.

**Causa:** o Grafana leva 30–60 segundos para inicializar completamente. O healthcheck tem `interval: 30s, retries: 3`, então se o primeiro check rodar antes dos 30s iniciais, o container entra em estado `unhealthy`.

**Verificação:** `curl http://192.168.10.104:3000/api/health` — se retornar `{"database": "ok"}`, o Grafana está funcionando. O estado `unhealthy` é residual do boot.

**Não é um bug de configuração.** O `start_period` poderia ser adicionado ao healthcheck para dar um período de graça, mas o comportamento atual é aceitável para lab.

### 19.2 File provisioning de dashboards quebrado no Grafana 13

**Sintoma:** dashboards provisionados via arquivo aparecem como "read-only" e não podem ser editados no UI.

**Causa:** o Grafana 13 introduziu Unified Storage que conflita com `allowUiUpdates: true` no provisioning.

**Solução:** desabilitar o file provisioning para dashboards e usar o workflow API + JSON manual (descrito na seção 13.4).

### 19.3 Promtail sem curl/wget

**Sintoma:** healthcheck de Promtail falha com `wget not found`.

**Causa:** a imagem `grafana/promtail:3.0.0` foi minimalizada e não inclui utilitários de rede.

**Solução:** usar bash com `/dev/tcp` para fazer requisições HTTP raw (descrito na seção 7.2).

### 19.4 kube-state-metrics NodePort vs port-forward

**Decisão:** usar `kubectl port-forward` persistente via systemd ao invés de NodePort.

**Por quê?** NodePort expõe uma porta em todos os nodes do cluster. No Minikube com single-node, o node IP é `192.168.49.2` — acessível apenas de dentro da VM docker. Mesmo com DNAT, o Prometheus na VM ansible tentaria acessar por `192.168.10.112:PORTA_NODEPORT` que passaria pelo DNAT errado. O port-forward com `--address 0.0.0.0` é mais simples e direto para este cenário.

### 19.5 CPU type kvm64 e pacotes el10

**Sintoma:** `dnf install zabbix-agent2` com repositório `el10` falha com SIGILL (instrução ilegal).

**Causa:** binários para RHEL 10 são compilados com instruções AVX-512 que o CPU type `kvm64` não emula.

**Solução:** usar repositório `el9` para todas as VMs Fedora 40 no HomeLAB.

### 19.6 Kubeconfig do Minikube fora da VM docker

**Problema:** o kubeconfig do Minikube usa IPs internos (`192.168.49.2`) e referencia arquivos de certificado que só existem na VM docker.

**Solução:** script Python que:
1. Copia o kubeconfig via SSH
2. Substitui o IP interno pelo IP externo
3. Embutida os certificados em base64 no próprio YAML
4. Ativa `insecure-skip-tls-verify` (o cert TLS não cobre o IP externo)

Detalhes em `docs/runbooks/RB-004-instalacao-kubernetes-mcp-server.md`.

---

> **Dúvidas ou divergências?** O `OVERVIEW.md` tem a visão macro do ambiente e o histórico de decisões. Os runbooks em `docs/runbooks/` cobrem os cenários de falha mais comuns.
