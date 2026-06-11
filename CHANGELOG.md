# CHANGELOG

---

## [2026-06-11] — Agente: correlação log × métrica (RCA) — v2.1.0

### O que mudou
- `scripts/zabbix_agent.py`: nova ferramenta `loki_query_range` (HTTP direto ao Loki, `GET /loki/api/v1/query_range`), exposta ao Gemini junto com `ssh_execute`.
- Nova fase `[3.5] CORRELACIONAR` no fluxo: alinha o instante da anomalia na métrica (Zabbix) com as linhas de log (Loki) numa janela temporal, montando uma timeline de causa raiz.
- JSON de saída do agente enriquecido: `root_cause`, `correlation_evidence` (timeline cronológica) e `confidence`.
- `incident-log.jsonl` e o postmortem auto-gerado passam a registrar causa raiz, confiança e timeline.
- `AGENT.md` atualizado para v2.1.0 (seção 2.3 Loki, fase de correlação no fluxo).
- `.env.zabbix-agent.example`: nova variável `LOKI_URL` (padrão `http://192.168.10.104:3100`).

### Impacto
- Sem breaking changes. A correlação é aditiva — se o Loki estiver inacessível, a tool retorna erro tratado e o agente segue com diagnóstico só de métrica (`confidence: baixa`).
- Requer `LOKI_URL` no `.env.zabbix-agent` (default já aponta para a VM `ansible`). Sem reinício de dados; apenas restart do processo do agente.

### Referências
- Loki HTTP API — `GET /loki/api/v1/query_range` (query, start, end, limit, direction).

---

## ⏸️ PONTO DE PARADA — 2026-06-10

### Estado atual

**Agente autônomo de incidentes Zabbix (`zabbix_agent.py` v2.0.0):**
- ✅ Rodando em background na workstation `192.168.10.108`, porta `9001` (PID 189011)
- ✅ Webhook Zabbix configurado: Media Type "Zabbix Agent Autônomo" (ID 70) + Action (ID 7)
- ✅ Conta `svc-zabbix` criada com sudo NOPASSWD em 6 VMs (104, 112, 210, 202, 203, 204)
- ✅ SSH funcional: `ssh svc-zabbix@192.168.10.210 "sudo systemctl restart zabbix-agent2"` → OK
- ✅ Acknowledge após 60s de persistência do incidente
- ✅ Padrão `agent is not available` detectado automaticamente → restart via SSH
- ✅ Todos os `.md` do projeto atualizados (CHANGELOG, AGENT.md, README.md)

**Fluxo completo configurado mas ainda não testado de ponta a ponta:**
```
Trigger Zabbix → Action 7 → Media Type 70 → POST 192.168.10.108:9001
  → aguarda 60s → acknowledge → Gemini Flash investiga
  → ssh_execute: sudo systemctl restart zabbix-agent2
  → problem_active_get (confirma resolução) → Telegram
```

### Para retomar — próximo passo imediato

**Teste de ponta a ponta (o que não foi feito ainda):**
```bash
# 1. Confirmar agente rodando
ps aux | grep zabbix_agent | grep -v grep
tail -20 logs/zabbix-agent.log

# 2. Se não estiver rodando, subir:
nohup python scripts/zabbix_agent.py --mode server --port 9001 >> logs/zabbix-agent.log 2>&1 &

# 3. Parar o zabbix-agent2 no MCP-Server para disparar o incidente
ssh svc-zabbix@192.168.10.210 "sudo systemctl stop zabbix-agent2"

# 4. Aguardar o Zabbix detectar (1-2 min) e disparar o webhook

# 5. Acompanhar o agente em ação:
tail -f logs/zabbix-agent.log

# 6. Verificar no Telegram e no dashboard do Zabbix
```

**O que esperar no teste:**
- `~t+0s` — webhook recebido, log: "Aguardando 60s..."
- `~t+60s` — `problem_active_get` confirma incidente ativo → acknowledge no Zabbix
- `~t+65s` — Gemini começa investigação: `host_get` → `ssh_execute restart` → `problem_active_get`
- `~t+90s` — Telegram: mensagem de resolução ou escalação
- Zabbix dashboard: evento reconhecido com "🤖 Agente autônomo assumiu..."

### Pendências conhecidas

- [ ] **Teste end-to-end** — ainda não executado (próximo passo)
- [ ] **WEBHOOK_SECRET** — está como `CHANGE_ME_MIN_32_CHARS` (funcional no lab, mas trocar por valor real antes de usar em produção)
- [ ] **Agente não persiste entre reboots** — não tem systemd service; precisa subir manualmente após reboot da workstation
- [ ] **Expandir SSH_ALLOWED_COMMANDS** — futuramente permitir restart de outros serviços (docker containers, zabbix-server, etc.) conforme os casos de uso surgirem

### Arquivos modificados hoje

| Arquivo | Mudança |
|---|---|
| `scripts/zabbix_agent.py` | Criado — agente completo v2.0.0 |
| `.env.zabbix-agent.example` | Criado — template de configuração |
| `AGENT.md` | Atualizado para v2.0.0 |
| `CHANGELOG.md` | Entrada de hoje adicionada |
| `README.md` | Seção do agente Zabbix adicionada |

---

## [2026-06-10] — Agente autônomo de incidentes Zabbix (zabbix_agent.py v2.0.0)

### O que mudou

**1. `scripts/zabbix_agent.py` — agente autônomo criado (v2.0.0)**
- Arquitetura: Webhook HTTP (porta 9001) → Gemini Flash 2.5 + MCP Zabbix → Telegram
- Loop ReAct com até 10 turnos; cada operação MCP abre/fecha conexão isolada (evita conflito asyncio)
- 12 ferramentas MCP expostas ao Gemini (`MCP_ALLOWED_TOOLS`) — subset seguro de 237 disponíveis
- Ferramenta customizada `ssh_execute`: executa comandos via SSH no host afetado, com allowlist de 8 comandos `systemctl` para `zabbix-agent2`/`zabbix-agent`
- Acknowledge no Zabbix após **60 segundos** de persistência do incidente (não imediato) — descarta silenciosamente se o problema se resolver sozinho antes disso
- Padrões conhecidos de incidente embutidos no system prompt: `agent is not available`, container caído, uso de recurso
- Detecção de padrão no payload: injeta hint diretamente no prompt do incidente
- Runbooks (`docs/runbooks/RB-*.md`) carregados integralmente no contexto do agente
- Regra anti-loop: nunca repetir a mesma chamada de ferramenta com os mesmos parâmetros
- Postmortem automático criado a partir do template quando `open_postmortem: true`
- Log em `logs/zabbix-agent.log`; incidente registrado em `docs/postmortem/incident-log.jsonl`

**2. Webhook Zabbix configurado**
- Media Type "Zabbix Agent Autônomo" criado (mediatypeid: 70) — script JS que faz POST para `http://192.168.10.108:9001` com header `X-Webhook-Secret`
- Action "Notificar Agente Autônomo de Incidentes" criada (actionid: 7) — dispara para eventos de trigger, envia via Media Type 70 ao usuário Admin
- Agente vinculado ao usuário Admin (userid: 1)

**3. Conta de serviço `svc-zabbix` criada em 6 VMs**
- Hosts: `ansible` (104), `docker` (112), `mcp-server` (210), `zabbix-server` (202), `zabbix-front` (203), `zabbix-proxy` (204)
- `sudoers.d/svc-zabbix`: NOPASSWD restrito a 8 comandos `systemctl restart/start/stop/status` de `zabbix-agent2` e `zabbix-agent`
- `Defaults:svc-zabbix !requiretty` para execução via SSH sem TTY interativo
- Chave pública `homelab_ed25519` instalada em `/home/svc-zabbix/.ssh/authorized_keys`
- Sem senha armazenada em nenhum arquivo do projeto

**4. Novos arquivos**
- `scripts/zabbix_agent.py` — agente principal
- `.env.zabbix-agent.example` — template com `GEMINI_API_KEY`, `GEMINI_MODEL`, `ZABBIX_MCP_URL/TOKEN`, `TELEGRAM_BOT_TOKEN/CHAT_ID`, `WEBHOOK_PORT/SECRET`, `SSH_USER=svc-zabbix`, `SSH_KEY_PATH`
- `logs/zabbix-agent.log` — log de operação (local)

### Impacto
- Incidentes Zabbix com severidade ≥ Warning são automaticamente investigados e, quando possível, resolvidos sem intervenção humana
- Para `agent is not available`: agente faz SSH como `svc-zabbix` e executa `sudo systemctl restart zabbix-agent2` — sem senha, sem privilégios excessivos
- Resultado sempre notificado no Telegram; postmortem criado automaticamente para incidentes graves

### Como iniciar o agente
```bash
# Instalar dependências
pip install mcp google-generativeai httpx python-dotenv

# Configurar
cp .env.zabbix-agent.example .env.zabbix-agent
# editar .env.zabbix-agent com valores reais

# Subir em background
nohup python scripts/zabbix_agent.py --mode server --port 9001 >> logs/zabbix-agent.log 2>&1 &

# Testar
python scripts/zabbix_agent.py --mode test
```

### Referências
- `scripts/zabbix_agent.py` — implementação completa
- `.env.zabbix-agent.example` — template de configuração
- `AGENT.md` — guardrails e spec atualizada (v2.0.0)

---

## [2026-06-04] — minikube-start.service: timeout aumentado para 600s

### O que mudou
- `minikube-start.service` na VM `docker` (192.168.10.112): `TimeoutStartSec` aumentado de `300s` para `600s`
- `docs/hosts.md`: tabela de serviços systemd atualizada para refletir o novo valor

### Causa raiz
No boot da VM, `minikube start` completou os addons em ~3min19s mas o passo final excedeu o limite de 300s do systemd. O serviço foi encerrado com `SIGTERM` e marcado como `failed (Result: timeout)`. Como o `painel-estudos-sre-portforward.service` depende do `minikube-start.service`, o port-forward também ficou inativo por dependência. O cluster Minikube em si subiu corretamente; apenas os serviços systemd ficaram em estado `failed`.

### Impacto
- No próximo boot, systemd aguarda até 10 minutos pelo `minikube start` — tempo suficiente para o cluster subir em qualquer condição de carga
- Port-forward do `painel-estudos-sre` (:8080) e `kube-state-metrics` (:32080) sobem automaticamente na sequência

### Referências
- Runbook criado: `docs/runbooks/RB-005-servicos-k8s-docker-vm.md`

---

## [2026-05-31] — README atualizado e diagrama ASCII adicionado

### O que mudou
- `README.md` atualizado: status, referência ao setup-guide, VM mcp-server com ambos os MCP Servers, seção Documentação, estrutura de diretórios refletindo `docs/` atual
- Criado `docs/homelab-diagram.png` — diagrama ASCII estilo matricial da arquitetura completa do lab
- Adicionados `docs/gen_diagram.py` e `docs/homelab-diagram.dot` ao repositório

### Impacto
- Nenhum — documentação apenas

---

## [2026-05-31] — Guia de setup completo criado

### O que mudou
- Criado `docs/setup-guide.md` (1496 linhas) — guia end-to-end do zero ao ambiente completo
- Cobre: Proxmox, SSH, Docker Compose, Prometheus, Loki, Promtail, OTEL Collector, Tempo, Grafana 13, Zabbix Agent 2, Minikube, manifestos K8s, port-forwards systemd, DNAT, MCP Servers
- Inclui seção de problemas conhecidos e decisões de design (18 tópicos)

### Impacto
- Nenhum — documentação apenas

---

## ⏸️ PONTO DE PARADA — 2026-05-29

### Estado atual da stack

**Stack de observabilidade (VM ansible — 192.168.10.104):**
- Prometheus, Grafana 13, Loki, Tempo, OTEL Collector, Promtail: ✅ todos operacionais (6 containers)
- Dashboards ativos: `graphviz-testes`, `kubernetes-overview`, `logs-overview`
- Healthcheck do Promtail corrigido (bash /dev/tcp — sem wget/curl na imagem)

**Kubernetes (VM docker — 192.168.10.112):**
- Minikube UP, `painel-estudos-sre` v1.2 em `1/1 Running`
- kube-state-metrics v2.19.0 operacional — coletado pelo Prometheus
- Port-forwards persistentes via systemd: `painel-estudos-sre` (8080) e `kube-state-metrics` (32080)
- DNAT persistente: `192.168.10.112:8443 → 192.168.49.2:8443` via systemd

**MCP Servers (VM mcp-server — 192.168.10.210):**
- Zabbix MCP Server v1.30 → porta 8080 ✅
- Kubernetes MCP Server v0.0.62 → porta 8081 ✅ (read-only, toolsets: core + config)

**Agente autônomo de SRE:**
- `AGENT.md` v1.0.0 criado — spec completa (guardrails, fluxo 7 passos, formatos Telegram/Google Chat)
- `scripts/agent_orchestrator.py` v1.0.0 criado — implementação completa
- `.env.agent.example` criado — template com todas as variáveis
- `.env.agent` adicionado ao `.gitignore`
- ⚠️ **Orquestrador NÃO testado ainda** — nenhum `--mode test` executado; nenhum systemd service criado para ele
- ⚠️ **Dependências não instaladas** — `pip install anthropic httpx python-dotenv` pendente no host que vai rodar o script

**Documentação:**
- Todos os `.md` sincronizados: README, OVERVIEW, CHANGELOG, CLAUDE.md, SECURITY.md, AGENT.md
- Scan de segurança executado — nenhuma credencial exposta nos arquivos a commitar

### Pendente na Fase 2

- [ ] **Alertmanager** — adicionar ao `docker-compose.yml` + `alerting.rules.yml` separado do `recording.rules.yml` (exigido pelo CLAUDE.md)
- [ ] **Node Exporter** — adicionar ao `docker-compose.yml` para métricas de host da VM `ansible`
- [ ] **Promtail no scrape do Prometheus** — porta 9080 ainda não configurada em `config/prometheus/prometheus.yml`
- [ ] **Testar agent_orchestrator.py** — `python scripts/agent_orchestrator.py --mode test --source zabbix`
- [ ] **Systemd service para o orchestrator** — rodar como serviço persistente em qual VM?

### Ações abertas dos postmortems (local — `docs/postmortem/`)

- INC-2026-05-27-001 ação #1 (vencida 2026-05-28): verificar outros hosts com problema de CPU preprocessing
- INC-2026-05-27-002 ação #2 (vencida 2026-05-28): verificar macros de hosts `ansible`, `docker`, `mcp-server` no proxy

### Para retomar

```bash
# 1. Confirmar stack de observabilidade
make status          # na VM ansible — esperar 6 containers Up

# 2. Confirmar Kubernetes
ssh 192.168.10.112
systemctl status minikube-start painel-estudos-sre-portforward kube-state-metrics-portforward minikube-apiserver-dnat

# 3. Confirmar MCP Servers
curl -s http://192.168.10.210:8080/health   # Zabbix MCP → {"status":"ok"}
curl -s http://192.168.10.210:8081/healthz  # K8s MCP → 200 OK

# 4. Confirmar targets do Prometheus
# http://192.168.10.104:9090/targets → todos UP
```

**Próximas tarefas sugeridas (por prioridade):**
1. Alertmanager + `alerting.rules.yml` no `docker-compose.yml`
2. Node Exporter no `docker-compose.yml`
3. Testar `agent_orchestrator.py` em modo test
4. Scrape do Promtail no Prometheus

---

## [2026-05-29] — Agente autônomo de SRE: AGENT.md, orchestrator e template de config

### O que mudou
- `AGENT.md` criado (v1.0.0) — especificação do agente autônomo de resposta a incidentes: guardrails (3 níveis), fluxo de 7 passos, inventário de criticidade por host, formatos de notificação Telegram/Google Chat, critérios de postmortem automático
- `scripts/agent_orchestrator.py` criado (v1.0.0) — implementação: webhook HTTP (porta 9000) → Claude API com MCP servers (Zabbix + Kubernetes) → Telegram/Google Chat + JSONL de incidentes + postmortem automático
- `.env.agent.example` criado — template de todas as variáveis do orquestrador (`ANTHROPIC_API_KEY`, MCP URLs, tokens de notificação, `WEBHOOK_SECRET`)
- `.gitignore`: `.env.agent` adicionado à lista de arquivos nunca commitados

### Impacto
- Agente pode ser iniciado com `python scripts/agent_orchestrator.py --mode server --port 9000`
- Modo teste disponível: `python scripts/agent_orchestrator.py --mode test --source zabbix`
- Dependências: `pip install anthropic httpx python-dotenv`

---

## [2026-05-29] — Sincronização geral da documentação

### O que mudou
- `README.md`: nomes dos runbooks corrigidos (B-001/B-002 → RB-001/RB-002); RB-004 adicionado; seção MCP expandida com Kubernetes MCP Server
- `OVERVIEW.md`: VM docker atualizada (port-forwards agora persistentes); K8s MCP Server adicionado; roadmap Fase 2 atualizado; RB-004 no guia de arquivos
- `CLAUDE.md`: estrutura de diretórios atualizada (Promtail, dashboards, RB-004, OVERVIEW.md)
- `scripts/health-check.sh`: Promtail (porta 9080) adicionado ao health check
- `docs/hosts.md`: Kubernetes MCP Server e serviços systemd da VM docker adicionados; runbooks e pendências atualizados

### Impacto
- Nenhum impacto na stack; alterações exclusivamente documentais

---

## [2026-05-29] — Port-forwards persistentes via systemd (VM docker)

### O que mudou
- `minikube-start.service` — inicia o Minikube no boot (oneshot, `After=docker.service`)
- `painel-estudos-sre-portforward.service` — port-forward persistente `:8080 → pod:80` (`After=minikube-start.service`, `Restart=on-failure`)
- `kube-state-metrics-portforward.service` — port-forward persistente `:32080 → pod:8080` (idem)
- Todos os serviços rodam como usuário `fedora`, com `ExecStartPre` aguardando os pods estarem `Ready`
- SELinux: `restorecon` aplicado nos três arquivos de serviço

### Impacto
- Port-forwards sobrevivem a reboots da VM `docker`; Prometheus mantém o target `kube-state-metrics` UP automaticamente

---

## [2026-05-29] — DNAT persistente para apiserver do Minikube (VM docker)

### O que mudou
- Serviço systemd `minikube-apiserver-dnat.service` criado na VM `docker` (192.168.10.112)
- Persiste a regra iptables DNAT `192.168.10.112:8443 → 192.168.49.2:8443` entre reboots
- Roda como oneshot após `docker.service` — garante ordem correta com as redes do Minikube
- Idempotente: verifica se a regra já existe antes de adicionar (`iptables -C`)

### Impacto
- Kubernetes MCP Server (porta 8081) passa a funcionar após reboot da VM `docker`

---

## [2026-05-29] — Instalação do Kubernetes MCP Server (RB-004)

### O que mudou
- Kubernetes MCP Server v0.0.62 instalado na VM `mcp-server` (192.168.10.210), porta 8081
- Modo `read_only = true`, toolsets: `core` + `config`, Secrets e ServiceAccounts bloqueados
- Kubeconfig do Minikube (192.168.10.112) copiado com certificados embutidos em base64 e `insecure-skip-tls-verify: true` (lab — cert não cobre IP externo)
- Regra iptables DNAT adicionada na VM `docker` para expor o apiserver (`192.168.10.112:8443 → 192.168.49.2:8443`)
- Porta 8081 aberta no firewalld da VM `mcp-server`
- Serviço systemd `kubernetes-mcp-server` habilitado e rodando como `nobody`

### Desvios em relação ao RB-004
- Nome do binário no runbook estava errado (`kubernetes-mcp-server_linux_amd64` → `kubernetes-mcp-server-linux-amd64`)
- Kubeconfig referenciava paths locais da VM docker — necessário embutir certs em base64
- IP interno do Minikube (`192.168.49.2`) não acessível externamente — adicionada regra DNAT + `insecure-skip-tls-verify`
- SELinux bloqueava o carregamento da unit — necessário `restorecon` no arquivo de serviço

### Impacto
- VM `mcp-server` agora tem dois MCPs: Zabbix (8080) + Kubernetes (8081)

### Referências
- https://github.com/containers/kubernetes-mcp-server/releases/tag/v0.0.62

---

## [2026-05-29] — CLAUDE.md: seção 10 "Guardrails do Agente Autônomo" adicionada

### O que mudou
- Nova seção `10. Guardrails do Agente Autônomo` no `CLAUDE.md`, inserida após as responsabilidades obrigatórias do agente
- Define ações autônomas permitidas (acknowledge Zabbix, coleta diagnóstico, restart OOM, delete pod CrashLoopBackOff, consultas MCP read-only)
- Define ações que exigem aprovação humana (qualquer ação em `zabbix-db`, scale down para 0, janelas de manutenção, operações destrutivas, ações multi-host, hosts fora do inventário)
- Inclui regra geral de escalonamento: dúvida = parada + documentação + escalonamento

### Impacto
- Sem impacto na stack; mudança exclusivamente documental/instrucional

---

## [2026-05-29] — Fix: healthcheck do Promtail corrigido (wget → bash /dev/tcp)

### O que mudou
- `docker-compose.yml`: healthcheck do serviço `promtail` substituído de `wget` (ausente na imagem) para `bash -c "exec 3<>/dev/tcp/localhost/9080 ..."` — usa TCP nativo do bash
- Container recreado na VM `ansible` — status voltou a `healthy`

### Impacto
- Nenhum breaking change; Promtail continuava coletando logs normalmente, apenas o Docker não conseguia aferir saúde do container

---

## ⏸️ PONTO DE PARADA — 2026-05-28

### Estado atual da stack

**Stack de observabilidade (VM ansible — 192.168.10.104):**
- Prometheus, Grafana 13, Loki, Tempo, OTEL Collector, Promtail: ✅ todos operacionais
- 6 containers rodando (`docker compose ps`)

**Grafana — dashboards ativos (pasta Lab):**
- `graphviz-testes` ✅ — topologia do lab em Mermaid (corrigido: era DOT, não funciona no plugin)
- `kubernetes-overview` ✅ — estado do cluster Minikube via kube-state-metrics
- `logs-overview` ✅ — logs ao vivo de todos os containers via Promtail/Loki
- File provisioning desabilitado (bug Grafana 13) — dashboards gerenciados via UI + API

**Kubernetes (VM docker — 192.168.10.112):**
- Minikube rodando, `painel-estudos-sre` v1.2 em `1/1 Running`
- `kube-state-metrics` v2.19.0 instalado via Helm no namespace `kube-system`
- Port-forwards ativos (não persistem após reboot):
  - `painel-estudos-sre`: `192.168.10.112:8080` → pod:80
  - `kube-state-metrics`: `192.168.10.112:32080` → pod:8080

**Prometheus — targets UP:**
- prometheus, grafana, loki, tempo, kube-state-metrics ✅

**Loki — logs recebidos de:** grafana, loki, otel-collector, prometheus, promtail, tempo ✅

### Pendente na Fase 2
- [ ] **Alertmanager** — adicionar ao `docker-compose.yml` e configurar roteamento de alertas
- [ ] **Node Exporter** — adicionar ao `docker-compose.yml` para métricas de host da VM `ansible`
- [ ] **Port-forward persistente** — criar systemd services na VM `docker` para `painel-estudos-sre` (8080) e `kube-state-metrics` (32080)

### Ações abertas dos postmortems (local — `docs/postmortem/`)
- INC-2026-05-27-002 ação #2 (vencida 2026-05-28): verificar macros de hosts `ansible`, `docker`, `mcp-server` no proxy
- INC-2026-05-27-001 ação #1 (vencida 2026-05-28): verificar outros hosts com problema de CPU preprocessing

### Para retomar
1. `make status` na VM `ansible` — confirmar 6 containers running
2. Verificar port-forwards na VM `docker`: `ps aux | grep port-forward`
3. Se port-forwards caíram, resubir:
   ```bash
   nohup kubectl port-forward -n apps svc/painel-estudos-sre-service 8080:80 --address 0.0.0.0 > /tmp/pf-painel.log 2>&1 &
   nohup kubectl port-forward -n kube-system svc/kube-state-metrics 32080:8080 --address 0.0.0.0 > /tmp/pf-ksm.log 2>&1 &
   ```
4. Confirmar todos os targets UP no Prometheus: `http://192.168.10.104:9090/targets`
5. Próxima tarefa sugerida: Alertmanager ou Node Exporter

---

## [2026-05-28] — Correção: file provisioning desabilitado, dashboards editáveis via UI

### O que mudou

**1. File provisioning de dashboards desabilitado**
- `allowUiUpdates: true` não funciona no Grafana 13 por bug no Unified Storage — dashboards continuavam bloqueados para edição na UI com a mensagem "This dashboard cannot be saved from the Grafana UI because it has been provisioned from another source"
- Solução: provider de arquivo comentado em `config/grafana/provisioning/dashboards/dashboards.yml`
- Dashboards agora vivem no banco do Grafana e podem ser editados livremente pelo admin

**2. Script `scripts/import-dashboards.sh` criado**
- Importa todos os JSONs de `config/grafana/dashboards/` para o Grafana via API
- Necessário em setup fresh (`make up` do zero) para restaurar os dashboards no banco
- Uso: `GRAFANA_PASS=<senha> ./scripts/import-dashboards.sh`

**Novo workflow de dashboards:**
1. Editar no Grafana → `Ctrl+S` (salva no banco do Grafana)
2. Para versionar: exportar via API → salvar no JSON → commitar
3. Em setup fresh: rodar `scripts/import-dashboards.sh`

### Impacto
- Admin consegue salvar dashboards normalmente via UI
- Dashboards não são mais sobrescritos pelo provisioner a cada 30s
- Em `make up` do zero, dashboards precisam ser importados manualmente via script

### Referências
- Bug: `allowUiUpdates: true` ignorado no Grafana 13 com Unified Storage backend
- Script: `scripts/import-dashboards.sh`

---

## [2026-05-28] — Promtail, dashboards Kubernetes e Logs, correções de provisioning

### O que mudou

**1. Promtail adicionado à stack (VM ansible — 192.168.10.104)**
- Imagem: `grafana/promtail:3.0.0`
- Coleta logs de todos os containers Docker via Docker socket (`/var/run/docker.sock`)
- Labels automáticos: `service`, `container`, `stream`, `compose_project`
- Envia para Loki em `http://loki:3100/loki/api/v1/push`
- Config em `config/promtail/promtail.yml`
- Serviços com logs visíveis: grafana, loki, prometheus, promtail, tempo, otel-collector

**2. Dashboard "Kubernetes — Visão Geral" criado**
- UID: `kubernetes-overview` — acessível em `http://192.168.10.104:3000/d/kubernetes-overview`
- 15 painéis: stats de cluster, pods por fase (donut), deployments (tabela), reinicializações de containers (time series), PVCs
- Datasource: Prometheus (`kube-state-metrics`)

**3. Dashboard "Logs — Stack de Observabilidade" criado**
- UID: `logs-overview` — acessível em `http://192.168.10.104:3000/d/logs-overview`
- Filtros por serviço e stream (variáveis de template)
- Painéis: volume por serviço, logs ao vivo, painéis individuais por serviço
- Datasource: Loki (`P8E80F9AEF21F6940`)

**4. Correção: graphviz-testes.json — DOT → Mermaid**
- Dashboard "GraphViz — Testes do Lab" retornava `Error rendering diagram. Check the diagram definition`
- Causa: `jdbranham-diagram-panel` v1.10.4 usa **Mermaid.js**, não GraphViz DOT
- Corrigido: conteúdo do painel reescrito em sintaxe Mermaid equivalente

**5. Correção: UIDs fixos nos datasources**
- Prometheus e Loki não tinham UID explícito em `datasources.yml` — Grafana gerava aleatoriamente a cada provisionamento
- Fixado: `uid: PBFA97CFB590B2093` (Prometheus) e `uid: P8E80F9AEF21F6940` (Loki)
- Dashboards JSON agora referenciam UIDs estáveis

**6. Correção: `allowUiUpdates: true` no provider de dashboards**
- Adicionado em `config/grafana/provisioning/dashboards/dashboards.yml`
- Permite salvar dashboards provisionados via UI sem precisar editar os JSONs manualmente

### Impacto
- Loki passou a receber logs reais de todos os containers da stack
- Grafana conta com 4 dashboards operacionais na pasta Lab
- Datasource UIDs estáveis — dashboards não quebram após restart do Grafana

### Referências
- Dashboard Kubernetes: `config/grafana/dashboards/kubernetes-overview.json`
- Dashboard Logs: `config/grafana/dashboards/logs-overview.json`
- Promtail config: `config/promtail/promtail.yml`

---

## [2026-05-27] — kube-state-metrics integrado ao Prometheus

### O que mudou

**1. kube-state-metrics instalado no Minikube (VM docker — 192.168.10.112)**
- Instalado via Helm: `helm install kube-state-metrics prometheus-community/kube-state-metrics --namespace kube-system`
- Versão do chart: `kube-state-metrics-7.4.0` (app: v2.19.0)
- Service do tipo `ClusterIP` convertido para `NodePort` na porta `32080`

**2. Port-forward exposto para a rede do lab**
- `kubectl port-forward -n kube-system svc/kube-state-metrics 32080:8080 --address 0.0.0.0` em background na VM `docker`
- Endpoint acessível em `http://192.168.10.112:32080/metrics`
- **Atenção:** port-forward não persiste após reboot da VM `docker` — avaliar systemd service

**3. Job `kube-state-metrics` adicionado ao Prometheus (VM ansible — 192.168.10.104)**
- Novo job em `config/prometheus/prometheus.yml` com target `192.168.10.112:32080`
- Target em estado `up` após reload

### Impacto
- Prometheus passa a coletar estado dos objetos Kubernetes (deployments, pods, PVCs, etc.)
- Base para criação de dashboards K8s no Grafana

### Referências
- Helm chart: `prometheus-community/kube-state-metrics`
- Namespace: `kube-system`

---

## [2026-05-27] — Resolução de falha de coleta do Proxmox VE após migração para Zabbix Proxy

### O que mudou

**1. SELinux — boolean `zabbix_can_network` habilitado no Zabbix Proxy (192.168.10.204)**
- O processo `zabbix_agent_t` no Fedora não pode fazer conexões TCP de saída por padrão
- Corrção: `setsebool -P zabbix_can_network 1` na VM zabbix-proxy
- Eliminado o alerta contínuo "Proxmox VE: API service not available"

**2. Macro `{$PVE.URL.PORT}` adicionada a nível de host no proxmox (host ID 10683)**
- Causa raiz: macro `{$PVE.URL.PORT}=8006` estava definida apenas no template "Proxmox VE by HTTP"; o Zabbix Proxy sincroniza apenas macros de host, não macros de template
- URLs dos 15 itens HTTP agent ficavam com macro não resolvida → HTTP agent poller com queue vazia (0 itens coletados)
- Correção: `usermacro.create` via API com `{$PVE.URL.PORT}=8006` no host 10683 (hostmacroid 7961)

**3. Zabbix Proxy reiniciado (192.168.10.204)**
- Necessário para recarregar a configuração em memória após a adição da macro
- Após restart: todos os ~99 itens dependentes voltaram a state=0 com valores corretos

### Impacto
- Métricas de CPU, memória, uptime e tráfego de rede de todas as VMs monitoradas no Proxmox voltaram a ser coletadas
- 0 itens em state=1 no host proxmox após a correção

### Referências
- Postmortem: `docs/postmortem/INC-2026-05-27-002.md`
- Host afetado: proxmox (192.168.10.254), proxy: zabbix-proxy (192.168.10.204)
- Template: "Proxmox VE by HTTP" (templateid 10517)

---

## ⏸️ PONTO DE PARADA — 2026-05-27 (sessão 2)

### Estado atual da Fase 2

**Stack de observabilidade (VM ansible — 192.168.10.104):**
- Prometheus, Grafana 13, Loki, Tempo e OTEL Collector: ✅ operacionais

**Zabbix (3 camadas):**
- Coleta via proxy para: ansible, docker, mcp-server ✅
- Proxmox VE monitorado via proxy (after SELinux + macro fix) ✅
- Falsos positivos de CPU (INC-2026-05-27-001): ✅ resolvido
- Coleta Proxmox sem dados (INC-2026-05-27-002): ✅ resolvido

**Documentação operacional criada (local — gitignored):**
- `docs/postmortem/postmortem.md` — template blameless
- `docs/postmortem/INC-2026-05-27-001.md` — falsos positivos CPU
- `docs/postmortem/INC-2026-05-27-002.md` — falha de coleta Proxmox pós-migração
- `docs/runbooks/` — RB-001, RB-002, RB-003
- `docs/hosts.md` — inventário completo do HomeLAB

**`.gitignore` atualizado:** `docs/hosts.md`, `docs/postmortem/`, `docs/runbooks/` excluídos do repositório (IPs internos e dados operacionais sensíveis).

### Pendente na Fase 2
- [ ] Alertmanager — adicionar ao `docker-compose.yml` e configurar roteamento
- [ ] Node Exporter — adicionar ao `docker-compose.yml` para métricas de host

### Para retomar
1. Verificar se todos os containers estão running: `make status` na VM ansible
2. Confirmar que o Zabbix proxy está ativo: `ssh 192.168.10.204` → `systemctl status zabbix-proxy`
3. Confirmar 0 problemas ativos no Zabbix Frontend (192.168.10.203)
4. Consultar `docs/postmortem/INC-2026-05-27-002.md` — ação #2 pendente para 2026-05-28: verificar macros dos outros hosts no proxy

---

## [2026-05-27] — Correção de falsos positivos de CPU no Zabbix + mudança de agentes para proxy

### O que mudou

**1. Zabbix Agent 2 — Server redirecionado para proxy (ansible, docker, mcp-server)**
- `Server` e `ServerActive` alterados de `192.168.10.202` (Zabbix Server) para `192.168.10.204` (Zabbix Proxy) nas três VMs
- Serviço `zabbix-agent2` reiniciado em todas

**2. Correção de falso positivo — "High CPU utilization" em docker e mcp-server**
- **Causa raiz:** item `system.cpu.util` no template "Linux by Zabbix agent" era um *dependent item* com preprocessing JavaScript (`return (100 - value)`). O Zabbix Server estava armazenando o valor bruto do master item (`system.cpu.util[,idle]`) sem aplicar o JavaScript, resultando em ~94-99% reportado como "utilização" quando era tempo ocioso.
- **Correção:** item `42267` no template convertido de *dependent + JavaScript* para *Calculated item* com fórmula `100-last(//system.cpu.util[,idle])` — não depende do Duktape/JavaScript engine.
- **Workaround temporário:** macro `{$CPU.UTIL.CRIT}=101` criada nos hosts `docker` (10686) e `mcp-server` (10684) para suprimir alertas enquanto a coleta era corrigida. Pode ser removida futuramente.

### Impacto
- Todos os 7 hosts que usam o template "Linux by Zabbix agent" passam a ter CPU utilization calculada corretamente
- Alertas falsos fechados automaticamente após correção
- Nenhum restart de serviço necessário no Zabbix Server

### Referências
- Hosts afetados: docker (192.168.10.112), mcp-server (192.168.10.210), ansible (192.168.10.104)
- Template modificado: "Linux by Zabbix agent" (ID 10001), item ID 42267

---

## ⏸️ PONTO DE PARADA — 2026-05-27

### Estado atual
- **painel-estudos-sre** rodando no Minikube (`pod 1/1 Running`, `PVC Bound`)
- Acessível em `http://192.168.10.112:8080/` via `kubectl port-forward`
- Port-forward ativo em background — **não persiste após reboot** da VM `docker`

### Para retomar
1. **Verificar se o pod ainda está rodando:**
   ```bash
   ssh 192.168.10.112
   kubectl get pods,pvc -n apps
   ```
2. **Se o port-forward caiu**, reativar:
   ```bash
   nohup kubectl port-forward -n apps svc/painel-estudos-sre-service 8080:80 --address 0.0.0.0 > /tmp/port-forward-sre.log 2>&1 &
   ```
3. **Próximos passos:**
   - Instrumentar `painel-estudos-sre` com OTEL SDK (push para `192.168.10.104:4317`)
   - Criar manifestos K8s do `focustrack` (requer secrets: Supabase + Gemini)
   - Adicionar Alertmanager e Node Exporter à stack do `ansible`

---

## [2026-05-27] — painel-estudos-sre: correções no Dockerfile e manifests; deploy validado

### O que mudou
- `Dockerfile` corrigido: typo `unicorn` → `uvicorn` no CMD
- Imagem rebuild e publicada como `rsantiag085/painel-estudos-sre:v1.2` no Docker Hub
- `deployment.yml` corrigido:
  - `mountPath` alterado de `/app/data` → `/data` (evita sobrepor o módulo Python `data/`)
  - `DATABASE_URL` atualizado para `sqlite:////data/sre_tracker.db`
- Deploy validado no Minikube: pod `1/1 Running`, PVC `Bound`, HTTP 200
- Acesso via `kubectl port-forward` exposto em `http://192.168.10.112:8080/` na rede local

### Impacto
- App funcional no Minikube — módulo `data/curriculum.py` acessível e banco persistido no PVC em `/data/`
- `port-forward` não persiste após reboot da VM — avaliar systemd service se necessário

---

## [2026-05-27] — painel-estudos-sre: manifests K8s prontos e imagem publicada no Docker Hub

### O que mudou
- `database.py` atualizado para ler `DATABASE_URL` via variável de ambiente (`os.getenv`), mantendo `sqlite:///./sre_tracker.db` como fallback local
- Imagem Docker `rsantiag085/painel_estudos_sre:v1.1` buildada e publicada no Docker Hub
- Criados manifestos Kubernetes em `/home/fedora/k8s-obs/painel-estudos-sre/` na VM `docker` (192.168.10.112):
  - `namespace/namespace.yml` — namespace `apps` com label `environment: lab`
  - `pvc/sqlite-pvc.yml` — PVC `1Gi`, `ReadWriteOnce`, `storageClassName: standard`
  - `deployment/painel-estudos-sre-deploy.yml` — 1 réplica, resources definidos, probes, `DATABASE_URL=sqlite:////data/sre_tracker.db`
  - `service/painel-estudos-sre-service.yml` — `NodePort`, porta 80→8000
- Histórico git do repositório `painel-estudos-sre` reescrito para remover conta corporativa (`rsantiago3c`) — todos os commits agora sob `rsantiag085`

### Impacto
- App pronta para deploy no Minikube via `kubectl apply`
- Dados do SQLite persistidos no PVC — sobrevivem a restarts do pod
- Repositório GitHub com contribuidor único (`rsantiag085`)

### Referências
- Docker Hub: `rsantiag085/painel_estudos_sre:v1.1`
- Manifestos: `~/k8s-obs/painel-estudos-sre/` na VM `docker` (192.168.10.112)

---

## [2026-05-27] — OTEL Collector adicionado à stack (Fase 2)

### O que mudou
- Adicionado serviço `otel-collector` ao `docker-compose.yml` (imagem `otel/opentelemetry-collector-contrib:0.102.0`)
- Criado `config/otel/otel-collector.yml` com pipelines para traces, logs e métricas
- Portas OTLP 4317 (gRPC) e 4318 (HTTP) movidas do Tempo para o OTEL Collector
- Tempo mantém escuta interna em 4317 (acessível via rede Docker); não expõe mais externamente

### Pipelines configurados
| Pipeline | Receiver | Exporter | Destino |
|----------|----------|----------|---------|
| traces   | OTLP     | otlp     | Tempo (interno :4317) |
| logs     | OTLP     | loki     | Loki `http://loki:3100/loki/api/v1/push` |
| metrics  | OTLP     | prometheusremotewrite | Prometheus `http://prometheus:9090/api/v1/write` |

### Impacto
- Apps no Minikube agora apontam para `192.168.10.104:4317` (gRPC) ou `:4318` (HTTP) — o Collector roteia internamente
- Apps não precisam mais conhecer Prometheus, Loki ou Tempo diretamente
- Health check disponível em `http://192.168.10.104:13133/`

### Referências
- `config/otel/otel-collector.yml` — configuração completa do Collector
- [OTEL Collector Contrib releases](https://github.com/open-telemetry/opentelemetry-collector-contrib/releases/tag/v0.102.0)

---

## [2026-05-25] — Remoção de referências ao agente de IA dos arquivos .md

### O que mudou
- `README.md` — seção MCP Server: título e descrição reescritos sem mencionar ferramenta específica
- `hosts.md` — observações e pendências reescritas com linguagem neutra ("cliente MCP")
- `CHANGELOG.md` — entradas anteriores ajustadas para remover nome do agente
- `OVERVIEW.md` — diagrama, seção 4.3 e roadmap reescritos com "agente de IA" genérico
- `CLAUDE.md` removido do rastreamento git (`git rm --cached`) — arquivo permanece local
- `.gitignore` — adicionada entrada para `CLAUDE.md` (instruções locais para agentes)

### Impacto
- Nenhum arquivo `.md` publicado no GitHub menciona o agente de IA utilizado
- `CLAUDE.md` continua funcional localmente para qualquer agente compatível

---

## ⏸️ PONTO DE RETOMADA — próxima sessão começa aqui

**Contexto:** todos os serviços do lab estão saudáveis. A VM docker (Minikube) está pronta para receber as primeiras aplicações.

**Próximos passos em ordem:**

1. **Criar o Dockerfile do `painel-estudos-sre`** (não tem ainda)
   - Python 3.13-slim, FastAPI + Uvicorn, porta 8000
   - O app usa SQLite em `./sre_tracker.db` — precisa de volume

2. **Criar os manifestos K8s** em `/home/fedora/k8s-obs/` na VM docker
   - Ordem: `namespace/` → `painel-estudos-sre/` → `focustrack/`
   - Começar pelo `painel-estudos-sre` (mais simples, sem deps externas)

3. **Instrumentar com OpenTelemetry**
   - Adicionar `opentelemetry-instrumentation-fastapi` no `requirements.txt`
   - Apontar para Tempo (`192.168.10.104:4317`) e Loki (`192.168.10.104:3100`)

4. **Validar pipeline completo no Grafana**
   - Traces do `painel-estudos-sre` visíveis no Tempo via Grafana
   - Depois repetir para o `focustrack`

**Arquivos de referência:**
- Estrutura K8s criada: `/home/fedora/k8s-obs/` (VM docker 192.168.10.112)
- Apps avaliadas: `/home/robson/projetos/painel-estudos-sre/` e `/home/robson/projetos/focustrack-ia-with-supabase/`
- Stack de observabilidade: VM ansible `192.168.10.104` — todos os 4 containers healthy

---

## [2026-05-25] — Estrutura K8s criada na VM docker e apps avaliadas para Minikube

### O que mudou
- Avaliados 4 projetos como candidatos a workloads no Minikube:
  - ✅ **`painel-estudos-sre`** — escolhido como primeiro deploy (FastAPI + SQLite, self-contained, sem deps externas)
  - 🟡 **`focustrack-ia-with-supabase`** — segundo candidato (Dockerfile pronto, mas depende de Supabase + Google AI)
  - ❌ `rs-finance` — frontend puro (Vite/JS), sem backend para deployar
  - ❌ `mapeamento-evangelico` — HTML estático, sem server nem telemetria
- Criada estrutura de pastas `/home/fedora/k8s-obs/` na VM `docker` (192.168.10.112):
  ```
  k8s-obs/
  ├── namespace/
  ├── painel-estudos-sre/  (configmap/ deployment/ service/ pvc/)
  └── focustrack/          (configmap/ secret/ deployment/ service/)
  ```

### Próximo passo
Criar o Dockerfile do `painel-estudos-sre` e os manifestos K8s — ver marcador **⏸️ PONTO DE RETOMADA** acima.

### Referências
- `hosts.md` — VM docker atualizada com `/k8s-obs`
- `OVERVIEW.md` — seção docker e roadmap atualizados

---

## [2026-05-25] — Criação do OVERVIEW.md — documentação completa do ambiente

### O que mudou
- Criado `OVERVIEW.md`: documento de referência completo para quem está conhecendo o ambiente pela primeira vez
- Cobre: objetivo do lab, arquitetura completa com diagrama ASCII, todas as VMs e serviços, integrações entre sistemas, cobertura do Zabbix Agent 2, URLs de acesso, roadmap e guia de arquivos

### Impacto
- Qualquer pessoa externa agora consegue entender o ambiente completo lendo um único arquivo

---

## [2026-05-25] — Zabbix Agent 2 instalado nas VMs ansible e docker; hosts adicionados ao Zabbix

### O que mudou
- Hosts criados no Zabbix via API:
  - `ansible` (ID `10687`) — IP `192.168.10.104:10050` | Template: Linux by Zabbix agent | Grupo: Zabbix servers
  - `docker` (ID `10686`) — IP `192.168.10.112:10050` | Template: Linux by Zabbix agent | Grupo: Zabbix servers
- **Zabbix Agent 2** v7.0.26 (el9) instalado em ambas as VMs (Fedora Linux 40)
  - Repo adicionado: `rhel/9` direto (evitando o problema de ISA do `el10` confirmado no zabbix-front)
  - `ansible`: `Server=192.168.10.202`, `Hostname=ansible` — firewalld: porta 10050/tcp aberta
  - `docker`: `Server=192.168.10.202`, `Hostname=docker` — firewalld desabilitado nesta VM
- Ambos os hosts confirmados com `available=1` via API do Zabbix

### Diagnóstico (ansible transitório)
- Primeiro check retornou `Received empty response... access permissions` — resolvido sozinho após ~30s (agent ainda inicializando)

### Impacto
- Todas as 7 VMs do HomeLAB agora monitoradas pelo Zabbix com template Linux by Zabbix agent
- `hosts.md` atualizado com tabelas de serviços para `ansible` e `docker`

### Referências
- `hosts.md` — seções `ansible` e `docker` atualizadas

---

## [2026-05-25] — Zabbix Agent 2 instalado no zabbix-front e host adicionado ao Zabbix

### O que mudou
- Host `zabbix-front` criado no Zabbix via API (hostid `10685`)
  - Template: **Linux by Zabbix agent** | Grupo: **Zabbix servers** | IP: `192.168.10.203:10050`
- **Zabbix Agent 2** v7.0.26 instalado na VM `zabbix-front` (Fedora Linux 40)
- ⚠️ **Problema diagnosticado:** repo Zabbix configurado para `rhel/10` — binário `el10` incompatível com CPU `QEMU 2.5+` (kvm64, sem AVX). Erro: `CPU ISA level is lower than required`
- **Solução:** repo corrigido de `rhel/10` → `rhel/9` em `/etc/yum.repos.d/zabbix.repo`; pacote `el9` instalado com sucesso
- `/etc/zabbix/zabbix_agent2.conf`: `Server=192.168.10.202`, `Hostname=zabbix-front`
- Firewalld: porta `10050/tcp` aberta
- Serviço `zabbix-agent2` habilitado no systemd e em execução

### Impacto
- VM `zabbix-front` agora monitorada pelo Zabbix com template **Linux by Zabbix agent**
- Interface disponível (`available: 1`) confirmada via API

### Observação sobre CPU
- **Todas as VMs com CPU tipo `kvm64` no Proxmox** podem ter este problema ao instalar pacotes compilados para RHEL 10 (x86-64-v2+). Usar sempre pacotes `el9` nesses hosts.

### Referências
- `hosts.md` — tabela de serviços do `zabbix-front` atualizada

---

## [2026-05-25] — Zabbix Agent 2 instalado no mcp-server e host adicionado ao Zabbix

### O que mudou
- Host `MCP-Server` criado no Zabbix via API (hostid `10684`)
  - Template: **Linux by Zabbix agent** | Grupo: **Zabbix servers** | IP: `192.168.10.210:10050`
- **Zabbix Agent 2** v7.0.26 instalado na VM `mcp-server` (RHEL 9.7) via repositório oficial Zabbix 7.0
- `/etc/zabbix/zabbix_agent2.conf` configurado: `Server=192.168.10.202`, `Hostname=MCP-Server`
- Firewalld: porta `10050/tcp` aberta | SELinux: label `zabbix_agent_port_t` já presente
- Serviço `zabbix-agent2` habilitado no systemd e em execução

### Impacto
- VM `mcp-server` agora monitorada pelo Zabbix com o template completo de métricas Linux
- Interface disponível (`available: 1`) confirmada via API do Zabbix

### Referências
- `hosts.md` — tabela de serviços do `mcp-server` atualizada com Zabbix Agent 2

---

## [2026-05-25] — Zabbix MCP Server instalado e integrado via MCP

### O que mudou
- `initMAX/zabbix-mcp-server` v1.30 instalado na VM `mcp-server` (192.168.10.210) via systemd
- Python 3.12 instalado automaticamente pelo installer (`--install-python`)
- Serviço `zabbix-mcp-server` ativo e saudável: `http://192.168.10.210:8080/health` → `{"status":"ok"}`
- Portal admin disponível em `http://192.168.10.210:9090/`
- Credenciais gerenciadas via `/etc/zabbix-mcp/.env` (chmod 600, nunca versionado)
- `config.toml` usa expansão de variáveis (`${ZABBIX_API_TOKEN}`, `${MCP_AUTH_TOKEN}`) — sem segredos em texto plano
- `EnvironmentFile=/etc/zabbix-mcp/.env` adicionado ao unit systemd para injetar segredos
- SELinux configurado: porta 8080 liberada com `semanage port -a -t http_port_t`
- Firewalld: portas 8080 e 9090 abertas
- MCP server registrado no cliente MCP local com autenticação Bearer token — disponível globalmente

### Impacto
- Cliente MCP agora tem acesso a 237 ferramentas do Zabbix via MCP (`zabbix: ✓ Connected`)
- Integração cobre: hosts, problemas, triggers, alertas, dashboards, templates, usuários, administração
- Zabbix API configurada em modo `read_only = true` (segurança)

### Referências
- Repositório: https://github.com/initMAX/zabbix-mcp-server
- `.env.mcp-server.example` — template local das variáveis do MCP server
- `hosts.md` — `mcp-server` listado com serviço ativo

---

## [2026-05-25] — Migração para autenticação SSH por chave em todas as VMs

### O que mudou
- Autenticação SSH dos hosts do HomeLAB migrada de senha para chave pública (ed25519)
- `~/.ssh/config` atualizado com a chave para todos os hosts do inventário
- Criado `.env.ssh` com IPs e usuário SSH (no `.gitignore`) para referência operacional
- `.gitignore` atualizado: `.env.ssh` e `.env.mcp-server` adicionados à lista de arquivos nunca commitados

### Impacto
- Elimina autenticação por senha na automação de comandos remotos (sem `sshpass`)

### Referências
- `.env.ssh` — mapa de IPs, usuário e detalhes da chave (local, não versionado)
- `SECURITY.md` — checklist atualizado com `.env.ssh`

---

## [2026-05-25] — Realocação de RAM entre VMs do HomeLAB

### O que mudou
- `mcp-server` (192.168.10.210): 4 GB → **2 GB RAM** (-2 GB)
- `docker` (192.168.10.112): 4 GB → **6 GB RAM** (+2 GB, recebido do mcp-server)
- `zabbix-front` (192.168.10.203): 4 GB → **2 GB RAM** (-2 GB)
- `ansible` (192.168.10.104): 4 GB → **6 GB RAM** (+2 GB, recebido do zabbix-front)

### Motivação
- VM `ansible` operava com 4 containers (Grafana + Prometheus + Loki + Tempo) em 4 GB — risco de pressão de memória sob carga
- VM `docker` executa Minikube, que demanda mais memória para workloads Kubernetes
- VMs doadoras (`mcp-server` e `zabbix-front`) têm cargas leves que toleram menos RAM

### Impacto
- Pressão de memória na stack de observabilidade resolvida (~572 MB em uso, 6 GB disponíveis)
- Minikube no `docker` com folga para executar workloads de exemplo (fonte futura de traces/métricas)
- `mcp-server` com 2 GB: validar se é suficiente quando o serviço MCP for iniciado
- `zabbix-front` com 2 GB: monitorar Apache + PHP-FPM sob carga de usuários simultâneos

### Referências
- `hosts.md` — inventário atualizado com nova configuração de RAM

---

## [2026-05-25] — Stack de observabilidade no ar e validação da infraestrutura

### O que mudou
- Stack subida na VM `ansible` (192.168.10.104): Grafana 13, Prometheus, Loki, Tempo — todos healthy
- Grafana acessível em `http://192.168.10.104:3000` (login validado via browser)
- Corrigido `cookie_secure = false` e `domain = 192.168.10.104` no `grafana.ini` (necessário para HTTP sem TLS)
- Datasources validados via API do Grafana: Prometheus ✅ | Loki ✅ | Tempo ✅
- Targets do Prometheus todos em estado `UP`: prometheus, grafana, loki, tempo
- Conectividade confirmada em todos os hosts: 201, 202, 203, 204, 210

### Descobertas sobre infraestrutura
- `zabbix-db` (201): **PostgreSQL** confirmado (porta 5432 aberta; MySQL 3306 fechada)
- `zabbix-front` (203): URL confirmada `http://192.168.10.203/zabbix/` (Apache 2.4.62, porta 80)
- `zabbix-proxy` (204): modo **passivo** confirmado (porta 10051 aberta)
- `mcp-server` (210): serviço MCP **não está rodando** — apenas SSH (porta 22) ativo

### Impacto
- Lab operacional para uso imediato — Grafana, Prometheus, Loki e Tempo funcionais
- `mcp-server` precisa ter o serviço MCP iniciado antes de validar integração com Zabbix

---

## [2026-05-25] — Mapeamento da infraestrutura e definição dos hosts do HomeLAB

### O que mudou
- Criado `hosts.md` com inventário completo do HomeLAB Proxmox (rede 192.168.10.0/24)
- Definida VM `ansible` (192.168.10.104) como host da stack de observabilidade
- Definida VM `docker` (192.168.10.112) como host exclusivo do Minikube/Kubernetes
- Documentada stack Zabbix em 3 camadas: db (201), server (202), front (203), proxy (204)
- Documentado `mcp-server` (192.168.10.210) como MCP Server para integração com Zabbix
- Fases do lab formalizadas: Fase 1 (atual) → Fase 2 → Fase 3

### Impacto
- Todos os arquivos de documentação atualizados para referenciar IPs reais da infraestrutura
- URLs de acesso aos serviços passam a usar `192.168.10.104` em vez de `localhost`

### Referências
- `hosts.md` — inventário completo dos hosts

---

## [2026-05-25] — Simplificação da stack para base de lab com minikube

### O que mudou
- Stack reduzida para 4 serviços: Prometheus, Grafana 13, Loki, Tempo
- Removidos: Promtail, OTEL Collector, Node Exporter, cAdvisor, Alertmanager, Nginx
- Removidos os diretórios: `config/alertmanager/`, `config/nginx/`, `config/otel/`, `config/promtail/`
- Removido `scripts/gen-certs.sh` (dependia do Nginx)
- `docker-compose.override.yml` simplificado para os 4 serviços restantes
- `config/prometheus/prometheus.yml` limpo: removidos scrape jobs dos serviços extintos e seção de alerting
- Adicionado job Zabbix comentado no prometheus.yml como placeholder para integração futura
- `.env.example` simplificado: removida variável `ALERTMANAGER_WEBHOOK_URL`
- `scripts/health-check.sh` atualizado para os 4 serviços
- `README.md` e `CLAUDE.md` atualizados para refletir a nova stack

### Motivação
Reduzir complexidade para facilitar iteração no lab e preparar base para evolução gradual com minikube.
O Zabbix (VM externa, 3 camadas) é o sistema de monitoramento principal — a integração será feita via exporter.

### Impacto
- Nenhum dado de log é coletado automaticamente (Promtail removido). Logs podem ser enviados diretamente ao Loki via API ou agente externo.
- Sem OTEL Collector: traces devem ser enviados diretamente ao Tempo (portas 14317/14318 expostas no override).
- Alertas desabilitados (Alertmanager removido). Pode ser reativado como evolução do lab.

---

## [2026-05-24] — Upgrade Grafana 11.0.0 → 13.0.0

### O que mudou
- Imagem do Grafana atualizada de `grafana/grafana:11.0.0` para `grafana/grafana:13.0.1`

### Impacto
- Verificar compatibilidade do plugin `jdbranham-diagram-panel` com Grafana 13
- Dashboards e datasources provisionados via código não devem ser afetados
- Recomendado executar `make down && make up` para recriar o container com a nova imagem

### Referências
- https://grafana.com/docs/grafana/latest/whatsnew/

## [2026-05-24] — Estrutura inicial do laboratório

### O que mudou
- Criação da estrutura de diretórios do lab
- Adição dos arquivos raiz: `docker-compose.yml`, `Makefile`, `.env.example`, `.gitignore`, `README.md`, `SECURITY.md`
- Stack inicial: Prometheus, Grafana, Loki, Tempo, Promtail, OTEL Collector, Node Exporter, cAdvisor, Alertmanager, Nginx

### Impacto
- Nenhum serviço rodando ainda. Lab requer `cp .env.example .env`, ajuste das variáveis e `make up`.

### Referências
- CLAUDE.md — especificação do lab
