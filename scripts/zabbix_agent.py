#!/usr/bin/env python3
"""
zabbix_agent.py — Agente Autônomo de Incidentes Zabbix
=======================================================
Versão   : 2.5.0
Criado em: 2026-06-10

Guardrails de atuador em CÓDIGO (não só no prompt):
  - ssh_execute: allowlist de host (nega zabbix-db e hosts fora do inventário) + de comando
  - script_execute: allowlist de scriptid (fail-closed via ZABBIX_ALLOWED_SCRIPT_IDS)
  - webhook: secret em tempo constante + cap de tamanho de corpo
  - process_incident resiliente: falha sempre vira notificação de escalação

Resiliência & concorrência (v2.3.0):
  - fila + pool de workers limitado (back-pressure 503); idempotência por eventid
  - deadline por chamada e por incidente; circuit breaker de quota (429)
  - scheduler dedicado para a espera de persistência (fora do pool de workers)

Fluxo:
  Webhook Zabbix → Gemini Flash → MCP Zabbix server → ferramentas Zabbix → Telegram

O agente conecta ao MCP server em runtime, descobre as ferramentas disponíveis
automaticamente e as expõe para o Gemini via function calling.

Dependências:
  pip install mcp google-genai httpx python-dotenv

Uso:
  python zabbix_agent.py --mode server --port 9001
  python zabbix_agent.py --mode test
"""

import argparse
import asyncio
import heapq
import hmac
import itertools
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from google import genai
from google.genai import types
import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# ---------------------------------------------------------------------------
# Configuração inicial
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent.parent / ".env.zabbix-agent")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent.parent / "logs" / "zabbix-agent.log"),
    ],
)
log = logging.getLogger("zabbix-agent")

# httpx loga a URL completa de cada request em nível INFO — incluindo o token
# do bot Telegram embutido no path. Silenciar para nunca vazar segredo no log.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuração via variáveis de ambiente
# ---------------------------------------------------------------------------

GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TURNS          = 10

# Deadlines — teto de tempo por chamada do Gemini e por incidente inteiro.
# Sem isto, uma chamada de rede travada pendura o worker indefinidamente.
LLM_CALL_TIMEOUT   = int(os.getenv("LLM_CALL_TIMEOUT", "60"))    # por chamada send_message
INCIDENT_DEADLINE  = int(os.getenv("INCIDENT_DEADLINE", "180"))  # wall-clock do incidente

# Circuit breaker — após N falhas de quota (429) seguidas, pausa as chamadas ao
# Gemini por um cooldown e escala direto, em vez de cada incidente falhar lento.
BREAKER_THRESHOLD  = int(os.getenv("BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN   = int(os.getenv("BREAKER_COOLDOWN", "300"))

ZABBIX_MCP_URL     = os.getenv("ZABBIX_MCP_URL", "http://192.168.10.210:8080/mcp")
ZABBIX_MCP_TOKEN   = os.getenv("ZABBIX_MCP_TOKEN", "")

# Kubernetes MCP — investigação de pods/workloads (VM mcp-server 192.168.10.210:8081).
# Vazio = desabilitado (fail-open). Servidor read-only (toolsets core + config).
K8S_MCP_URL        = os.getenv("K8S_MCP_URL", "")
K8S_MCP_TOKEN      = os.getenv("K8S_MCP_TOKEN", "")

# Context7 MCP — documentação oficial sob demanda (fase 3.6: enriquecer escalações).
# Vazio = desabilitado (fail-open): o agente segue sem a fase de doc.
CONTEXT7_MCP_URL       = os.getenv("CONTEXT7_MCP_URL", "")
CONTEXT7_API_KEY       = os.getenv("CONTEXT7_API_KEY", "")
CONTEXT7_ALLOWED_TOOLS = {"resolve-library-id", "query-docs"}

# Loki — fonte de logs para correlação log × métrica (RCA)
LOKI_URL           = os.getenv("LOKI_URL", "http://192.168.10.104:3100")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

WEBHOOK_PORT       = int(os.getenv("WEBHOOK_PORT", "9001"))
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")

# Concorrência — pool de workers limitado (anti-tempestade de alertas)
AGENT_WORKERS      = int(os.getenv("AGENT_WORKERS", "2"))
AGENT_QUEUE_MAX    = int(os.getenv("AGENT_QUEUE_MAX", "50"))

# Idempotência — janela (s) em que um eventid já processado é ignorado se reentregue
DEDUP_TTL          = int(os.getenv("DEDUP_TTL", "600"))

# Cooldown semântico — evita re-investigação de flapping (mesmo trigger/host
# abrindo e fechando ao redor do threshold). Janela (s) após a última investigação
# em que novas ocorrências do par (host, trigger) recebem só uma notificação curta,
# sem re-rodar o loop ReAct + LLM. Ao expirar, a próxima ocorrência é investigada
# normalmente (causa raiz pode ter mudado).
INCIDENT_COOLDOWN       = int(os.getenv("INCIDENT_COOLDOWN", "7200"))   # 2 horas
RECURRING_ESCALATION_AT = int(os.getenv("RECURRING_ESCALATION_AT", "3")) # Nª ocorrência → 🔴

# Espera de persistência (s) antes de assumir o incidente — feita FORA do pool
# de workers, por uma thread scheduler dedicada (não ocupa slot de worker).
PERSISTENCE_WAIT   = int(os.getenv("PERSISTENCE_WAIT", "60"))

SSH_USER           = os.getenv("SSH_USER", "svc-zabbix")
SSH_KEY_PATH       = os.getenv("SSH_KEY_PATH", str(Path.home() / ".ssh" / "homelab_ed25519"))

# Ferramentas MCP expostas ao Gemini — subset relevante para SRE/incidentes
MCP_ALLOWED_TOOLS = {
    "host_get", "host_status_get",
    "problem_get", "problem_active_get",
    "event_get", "event_acknowledge",
    "item_get", "item_history_summary_get",
    "history_get",
    "trigger_get",
    "alert_get",
    "infrastructure_summary_get",
    "script_get", "script_execute",
}

# Comandos SSH de AÇÃO permitidos — guardrail de segurança
SSH_ALLOWED_COMMANDS = {
    "sudo systemctl restart zabbix-agent2",
    "sudo systemctl restart zabbix-agent",
    "sudo systemctl start zabbix-agent2",
    "sudo systemctl start zabbix-agent",
    "sudo systemctl stop zabbix-agent2",
    "sudo systemctl stop zabbix-agent",
    "sudo systemctl status zabbix-agent2",
    "sudo systemctl status zabbix-agent",
    "systemctl restart zabbix-agent2",
    "systemctl restart zabbix-agent",
    "systemctl start zabbix-agent2",
    "systemctl start zabbix-agent",
    "systemctl status zabbix-agent2",
    "systemctl status zabbix-agent",
}

# Comandos SSH de DIAGNÓSTICO read-only — nenhum altera estado.
# Para journalctl e tail do Zabbix, adicionar ao sudoers do svc-zabbix:
#   sudo journalctl --no-pager -n 0 -u zabbix-server  → testar acesso
#   echo 'svc-zabbix ALL=(ALL) NOPASSWD: /usr/bin/journalctl, /usr/bin/tail' \
#     >> /etc/sudoers.d/svc-zabbix
SSH_DIAGNOSE_COMMANDS = {
    # CPU / processos
    "top -bn1 | head -25",
    "ps aux --sort=-%cpu | head -20",
    "ps aux --sort=-%mem | head -20",
    "ps -eo pid,user,%mem,%cpu,comm --sort=-%mem | head -30",
    "ps -eo pid,user,%mem,%cpu,args --sort=-%mem | head -20",
    "uptime",
    # Memória / disco / rede
    "free -h",
    "df -h",
    "ss -s",
    # Logs do sistema (journalctl sem sudo — vê apenas logs visíveis ao usuário)
    "journalctl -p err --since '30 min ago' --no-pager -n 100",
    "journalctl -p err --since '1 hour ago' --no-pager -n 100",
    "journalctl --since '30 min ago' --no-pager -n 100",
    # Zabbix Server service log
    "journalctl -u zabbix-server --since '30 min ago' --no-pager -n 100",
    "journalctl -u zabbix-server --since '1 hour ago' --no-pager -n 100",
    "journalctl -u zabbix-agent2 --since '30 min ago' --no-pager -n 50",
    # Arquivo de log do Zabbix (pode precisar de sudo no sudoers)
    "sudo tail -n 100 /var/log/zabbix/zabbix_server.log",
    "sudo tail -n 200 /var/log/zabbix/zabbix_server.log",
    "sudo tail -n 50 /var/log/zabbix/zabbix_agentd.log",
    # Kernel / hardware
    "dmesg | tail -30",
    # Proxmox VE — journald é o único backend de log (sem syslog)
    "journalctl -u pvestatd --since '30 min ago' --no-pager -n 100",
    "journalctl -u pvestatd --since '1 hour ago' --no-pager -n 100",
    "journalctl -u pvedaemon --since '30 min ago' --no-pager -n 100",
    "journalctl -u pve-cluster --since '30 min ago' --no-pager -n 100",
    "sudo tail -n 100 /var/log/pve-firewall.log",
}

# Hosts onde ssh_execute pode atuar — espelha o inventário do AGENT.md §7 / docs/hosts.md.
# Guardrail em CÓDIGO: o LLM propõe o host_ip, mas só estes são autorizados.
SSH_ALLOWED_HOSTS = {
    "192.168.10.104",  # ansible
    "192.168.10.112",  # docker
    "192.168.10.210",  # mcp-server
    "192.168.10.202",  # zabbix-server
    "192.168.10.203",  # zabbix-front
    "192.168.10.204",  # zabbix-proxy
    "192.168.10.254",  # proxmox (hypervisor)
}
# zabbix-db é deliberadamente EXCLUÍDO da allowlist — criticidade crítica,
# nenhuma ação autônoma (AGENT.md §3.3). Listado à parte para mensagem de erro clara.
SSH_DENIED_HOSTS = {
    "192.168.10.201",  # zabbix-db
}

# Scripts Zabbix que o agente pode executar autonomamente (IDs separados por vírgula
# no .env.zabbix-agent). VAZIO = nenhum script permitido (fail-closed): script_execute
# fica bloqueado em código até o operador habilitar IDs específicos.
ZABBIX_ALLOWED_SCRIPT_IDS = {
    s.strip() for s in os.getenv("ZABBIX_ALLOWED_SCRIPT_IDS", "").split(",") if s.strip()
}

# Tamanho máximo do corpo do webhook — payload de incidente é pequeno (anti-DoS).
MAX_BODY_BYTES = 256 * 1024  # 256 KB

AGENT_MD_PATH      = Path(__file__).parent.parent / "AGENT.md"
HOSTS_MD_PATH      = Path(__file__).parent.parent / "docs" / "hosts.md"
RUNBOOKS_PATH      = Path(__file__).parent.parent / "docs" / "runbooks"
INCIDENT_LOG       = Path(__file__).parent.parent / "docs" / "postmortem" / "incident-log.jsonl"
POSTMORTEM_TPL     = Path(__file__).parent.parent / "docs" / "postmortem" / "postmortem.md"
POSTMORTEM_DIR     = Path(__file__).parent.parent / "docs" / "postmortem"

# ---------------------------------------------------------------------------
# Conversão de Tool MCP → FunctionDeclaration (Gemini)
# ---------------------------------------------------------------------------

def _mcp_tool_to_gemini(tool) -> types.FunctionDeclaration:
    """Converte um MCP Tool para types.FunctionDeclaration.

    O SDK google-genai aceita JSON Schema diretamente via `parameters_json_schema`,
    então o inputSchema do MCP (já em JSON Schema) é repassado sem conversão.
    """
    input_schema = tool.inputSchema or {}
    kwargs = {"name": tool.name, "description": tool.description or ""}
    if input_schema.get("properties"):
        kwargs["parameters_json_schema"] = input_schema
    return types.FunctionDeclaration(**kwargs)

# ---------------------------------------------------------------------------
# Execução SSH — para hosts com Zabbix agent indisponível
# ---------------------------------------------------------------------------

import subprocess as _subprocess

_SSH_TOOL_DECLARATION = types.FunctionDeclaration(
    name="ssh_execute",
    description=(
        "Executa um comando via SSH em um host remoto do inventário. "
        "Use quando o Zabbix agent estiver indisponível e for necessário reiniciar o serviço. "
        "Comandos permitidos: sudo systemctl restart/start/stop/status zabbix-agent2 (ou zabbix-agent)."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "host_ip": {"type": "string", "description": "IP do host alvo, ex: 192.168.10.210"},
            "command": {"type": "string", "description": "Comando a executar, ex: sudo systemctl restart zabbix-agent2"},
        },
        "required": ["host_ip", "command"],
    },
)


_SSH_DIAGNOSE_DECLARATION = types.FunctionDeclaration(
    name="ssh_diagnose",
    description=(
        "Coleta diagnóstico read-only via SSH: logs do sistema, processos e uso de recursos. "
        "Use na fase [3.5b] quando Loki não retornar logs suficientes para determinar a causa raiz.\n"
        "Comandos disponíveis (passe os relevantes para o problema):\n"
        "  CPU/procs: 'top -bn1 | head -25'  |  'ps aux --sort=-%cpu | head -20'  |  'uptime'\n"
        "  Memória: 'free -h'  |  'ps aux --sort=-%mem | head -20'\n"
        "  Disco/rede: 'df -h'  |  'ss -s'\n"
        "  Logs sistema: 'journalctl -p err --since \\'30 min ago\\' --no-pager -n 100'\n"
        "  Zabbix svc: 'journalctl -u zabbix-server --since \\'30 min ago\\' --no-pager -n 100'\n"
        "  Log arquivo: 'sudo tail -n 100 /var/log/zabbix/zabbix_server.log'\n"
        "  Kernel: 'dmesg | tail -30'"
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "host_ip": {"type": "string", "description": "IP do host alvo, ex: 192.168.10.202"},
            "commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista de comandos do allowlist a executar. Escolha os relevantes para o incidente.",
            },
        },
        "required": ["host_ip", "commands"],
    },
)


def _ssh_diagnose(host_ip: str, commands: list) -> str:
    """Executa uma lista de comandos read-only via SSH e retorna a saída concatenada."""
    host_ip = host_ip.strip()
    if host_ip in SSH_DENIED_HOSTS:
        return f"ERRO: host {host_ip} é crítico (zabbix-db) — diagnóstico SSH bloqueado."
    if host_ip not in SSH_ALLOWED_HOSTS:
        return f"ERRO: host {host_ip} fora do inventário — diagnóstico SSH bloqueado."
    if not commands:
        return "ERRO: lista de comandos vazia."

    parts = []
    for cmd in commands[:6]:   # limite de 6 comandos por chamada
        cmd = cmd.strip()
        if cmd not in SSH_DIAGNOSE_COMMANDS:
            parts.append(f"# {cmd}\nERRO: comando fora do allowlist diagnóstico.")
            continue
        ssh_cmd = [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            f"{SSH_USER}@{host_ip}", cmd,
        ]
        log.info(f"SSH-diag → {SSH_USER}@{host_ip}: {cmd}")
        try:
            r = _subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=20)
            out = (r.stdout + r.stderr).strip()
            parts.append(f"# {cmd}\n{out or '(sem saída)'}")
        except _subprocess.TimeoutExpired:
            parts.append(f"# {cmd}\nERRO: timeout SSH")
        except Exception as e:
            parts.append(f"# {cmd}\nERRO: {e}")

    raw = "\n\n".join(parts)
    # Trunca para não estourar o contexto do modelo (~6 KB)
    if len(raw) > 6000:
        raw = raw[:6000] + "\n... [truncado]"
    return raw


def _ssh_run(host_ip: str, command: str) -> str:
    host_ip = host_ip.strip()
    command = command.strip()
    # Guardrail de host (antes do comando): nunca atuar no zabbix-db nem em host
    # fora do inventário — a proibição do AGENT.md vira regra de código, não só prompt.
    if host_ip in SSH_DENIED_HOSTS:
        log.warning(f"SSH BLOQUEADO → host crítico {host_ip} (zabbix-db)")
        return (
            f"ERRO: host {host_ip} é de criticidade crítica (zabbix-db) — "
            "ação autônoma proibida pelo AGENT.md §3.3. Escale para o SRE."
        )
    if host_ip not in SSH_ALLOWED_HOSTS:
        log.warning(f"SSH BLOQUEADO → host fora do inventário: {host_ip}")
        allowed_hosts = ", ".join(sorted(SSH_ALLOWED_HOSTS))
        return (
            f"ERRO: host {host_ip} fora do inventário autorizado. "
            f"Hosts permitidos: {allowed_hosts}. Escale para o SRE."
        )
    if command not in SSH_ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(SSH_ALLOWED_COMMANDS))
        return f"ERRO: Comando não permitido: {command!r}\nPermitidos: {allowed}"
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",   # evita testar todas as chaves do agente SSH
        f"{SSH_USER}@{host_ip}",
        command,
    ]
    log.info(f"SSH → {SSH_USER}@{host_ip}: {command}")
    try:
        result = _subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
        output = (result.stdout + result.stderr).strip()
        status = f"exit={result.returncode}"
        return f"{output}\n[{status}]" if output else f"[{status}]"
    except _subprocess.TimeoutExpired:
        return "ERRO: timeout SSH (10s)"
    except Exception as e:
        return f"ERRO SSH: {e}"

# ---------------------------------------------------------------------------
# Consulta Loki — correlação log × métrica (RCA)
# ---------------------------------------------------------------------------

_LOKI_TOOL_DECLARATION = types.FunctionDeclaration(
    name="loki_query_range",
    description=(
        "Consulta logs no Loki (LogQL) numa janela de tempo para correlacionar com a métrica "
        "do incidente e descobrir a CAUSA RAIZ. A métrica diz O QUÊ/QUANDO quebrou; o log diz POR QUÊ. "
        "Sempre filtre por container/host e por nível de erro. "
        "Exemplos de query: '{container=\"obs-loki\"} |~ \"(?i)error|fatal|panic|oom\"' "
        "ou '{host=\"ansible\"} | logfmt | level=\"error\"'."
    ),
    parameters_json_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": 'Expressão LogQL. Ex: {container="obs-loki"} |~ "(?i)error|fatal"'},
            "minutes": {"type": "integer", "description": "Janela de lookback em minutos a partir de end_iso (padrão 15)."},
            "end_iso": {"type": "string", "description": "Fim da janela em ISO8601 (padrão: agora). Use o timestamp do incidente para centrar a janela na anomalia."},
            "limit": {"type": "integer", "description": "Máximo de linhas a retornar (padrão 100)."},
        },
        "required": ["query"],
    },
)


def _loki_query_range(query: str, minutes: int = 15, end_iso: str = "", limit: int = 100) -> str:
    """Consulta /loki/api/v1/query_range numa janela e retorna as linhas ordenadas no tempo."""
    if not query or not query.strip():
        return "ERRO: parâmetro 'query' (LogQL) é obrigatório."
    try:
        end = datetime.fromisoformat(end_iso) if end_iso else datetime.now()
    except ValueError:
        end = datetime.now()
    end_ns   = int(end.timestamp() * 1_000_000_000)
    start_ns = int((end.timestamp() - minutes * 60) * 1_000_000_000)
    params = {
        "query":     query,
        "start":     str(start_ns),
        "end":       str(end_ns),
        "limit":     str(limit),
        "direction": "backward",
    }
    log.info(f"Loki → query_range {query!r} janela={minutes}min")
    try:
        resp = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        return f"ERRO ao consultar Loki: {e}"

    streams = resp.json().get("data", {}).get("result", [])
    if not streams:
        return f"Nenhuma linha de log encontrada para a query no intervalo de {minutes}min."

    lines = []
    for s in streams:
        labels = s.get("stream", {})
        ident = (
            labels.get("container")
            or labels.get("service_name")
            or labels.get("job")
            or labels.get("host")
            or "?"
        )
        for ts, line in s.get("values", []):
            t = datetime.fromtimestamp(int(ts) / 1e9).strftime("%H:%M:%S")
            lines.append((ts, f"[{t}] ({ident}) {line}"))

    lines.sort(key=lambda x: x[0])
    rendered = [l for _, l in lines]
    # Limita o retorno para não estourar o contexto do modelo
    if len(rendered) > 80:
        omitted = len(rendered) - 80
        rendered = rendered[:40] + [f"... ({omitted} linhas omitidas) ..."] + rendered[-40:]
    return "\n".join(rendered)

# ---------------------------------------------------------------------------
# Guardrail de script_execute — atuador privilegiado do Zabbix
# ---------------------------------------------------------------------------

def _script_execute_guard(args: dict) -> str | None:
    """
    Autoriza (ou não) uma chamada de script_execute ANTES de ela chegar ao MCP.
    Retorna uma mensagem de ERRO se a execução for proibida, ou None se permitida.

    Fail-closed: sem ZABBIX_ALLOWED_SCRIPT_IDS definido, nenhum script é executável.
    O LLM propõe o script; o código autoriza.
    """
    scriptid = str(args.get("scriptid", "")).strip()
    if not ZABBIX_ALLOWED_SCRIPT_IDS:
        log.warning("script_execute BLOQUEADO → allowlist vazia (fail-closed)")
        return (
            "ERRO: script_execute está desabilitado — nenhum script na allowlist. "
            "Defina ZABBIX_ALLOWED_SCRIPT_IDS no .env.zabbix-agent para habilitar "
            "scripts específicos. Escale para o SRE."
        )
    if scriptid not in ZABBIX_ALLOWED_SCRIPT_IDS:
        log.warning(f"script_execute BLOQUEADO → scriptid {scriptid!r} fora da allowlist")
        allowed = ", ".join(sorted(ZABBIX_ALLOWED_SCRIPT_IDS))
        return (
            f"ERRO: scriptid {scriptid!r} não está autorizado. "
            f"Scripts permitidos: {allowed}. Escale para o SRE."
        )
    return None

# ---------------------------------------------------------------------------
# Carregamento de contexto
# ---------------------------------------------------------------------------

def load_context() -> str:
    parts = []
    if AGENT_MD_PATH.exists():
        parts.append(f"# AGENT.md\n{AGENT_MD_PATH.read_text()}")
    if HOSTS_MD_PATH.exists():
        parts.append(f"# hosts.md\n{HOSTS_MD_PATH.read_text()}")
    if RUNBOOKS_PATH.exists():
        for rb_file in sorted(RUNBOOKS_PATH.glob("RB-*.md")):
            parts.append(f"# Runbook: {rb_file.name}\n{rb_file.read_text()}")
    return "\n\n---\n\n".join(parts)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _doc_phase_block() -> str:
    """Bloco da fase [3.6] CONSULTAR DOC — incluído só quando o Context7 MCP está ativo."""
    if not CONTEXT7_MCP_URL:
        return ""
    return """
---

## FASE [3.6] CONSULTAR DOC (Context7) — antes de escalar SEM causa clara

Quando você for ESCALAR sem causa raiz confiável (`confidence: baixa`, sem runbook aplicável,
ou host fora do inventário e portanto sem ação possível), NÃO entregue só "não sei". Consulte a
documentação oficial da ferramenta e entregue a escalação MASTIGADA:

1. `resolve-library-id` com o nome da ferramenta do incidente (ex: "Proxmox VE", "Grafana Loki",
   "Prometheus", "Kubernetes", "Docker") → obtenha o ID da biblioteca.
2. `query-docs` com esse ID e a pergunta específica do incidente.
3. Sintetize, ANCORADO nos trechos retornados:
   - `possible_causes`: causas prováveis, em ordem de probabilidade.
   - `suggested_checks`: comandos/verificações READ-ONLY para o HUMANO rodar (você NUNCA os executa).
   - `references`: seções/URLs da doc citadas.
4. Marque tudo como HIPÓTESE a verificar — não é causa confirmada; a `confidence` segue refletindo sua certeza real.

Mapa incidente → biblioteca: pve/Proxmox → "Proxmox VE"; obs-loki → "Grafana Loki";
obs-prometheus → "Prometheus"; obs-tempo → "Grafana Tempo"; grafana → "Grafana";
pod/minikube/k8s → "Kubernetes"; container/docker → "Docker". Se não souber, deixe o
resolve-library-id deduzir pelo texto do incidente.

A fase [3.6] é ANÁLISE-ONLY: enriquece a escalação, nunca dispara ação.
"""


def build_system_prompt(context: str) -> str:
    doc_phase = _doc_phase_block()
    return f"""Você é um agente autônomo de SRE especializado em resposta a incidentes Zabbix.
Seu comportamento é definido pelo AGENT.md abaixo. Leia-o integralmente antes de agir.

{context}

---

## PADRÕES CONHECIDOS DE INCIDENTE

### Padrão: "Zabbix agent is not available" / "agente indisponível"
Quando o problema ou trigger contiver "agent is not available", "agente indisponível" ou variações:
- **NÃO** tente consultar items ou histórico do host afetado — o agente Zabbix está DOWN e não responderá a nenhuma coleta ativa.
- Não repita chamadas de item_get ou history_get para o mesmo host.
- Fluxo obrigatório:
  1. `host_get` → obter o IP do host afetado.
  2. `ssh_execute` com `host_ip=<IP obtido>` e `command="sudo systemctl restart zabbix-agent2"` → tenta reiniciar o serviço.
  3. Aguarde mentalmente ~30s e chame `problem_active_get` para verificar se o problema foi resolvido.
  4. Se resolvido → `resolved: true`, `escalated: false`, descreva o restart no `action_taken`.
  5. Se persistir → verifique `sudo systemctl status zabbix-agent2` via `ssh_execute` para diagnóstico, depois escale com evidências.

### Padrão: container/serviço caído
Quando o problema indicar container ou serviço parado:
- Use `item_history_summary_get` para confirmar o último estado do item de status do container.
- Use `problem_active_get` para ver se há outros serviços do mesmo host afetados.
- Reinicie o container apenas se exit code = 137 (OOM kill) conforme AGENT.md.

### Padrão: uso de recurso (CPU, memória, disco)
Quando o problema indicar alta utilização de recurso:
- Use `history_get` ou `item_history_summary_get` para obter a tendência recente.
- Correlacione com outros itens do mesmo host.
- NÃO reinicie serviços por causa de uso de recurso sem diagnóstico de causa raiz.

### Padrão: pod / Kubernetes (Minikube na VM docker)
Quando o problema mencionar pod, Kubernetes, Minikube, CrashLoopBackOff, OOMKilled ou ImagePullBackOff:
- Se houver ferramentas de Kubernetes disponíveis (pods/list, events/list, pods/log e similares),
  USE-AS para diagnosticar: liste o pod, leia os eventos e os logs antes de concluir.
- Delete de pod só é permitido conforme AGENT.md (CrashLoopBackOff > 5 min, confirmado nos eventos).
- Se não houver ferramenta de ação aplicável, escale — e nesse caso use a fase de doc (Context7) para
  entregar possíveis causas + verificações `kubectl` ao humano.

---

## FASE DE CORRELAÇÃO LOG × MÉTRICA (RCA) — obrigatória antes de DECIDIR

A métrica diz O QUÊ quebrou e QUANDO; o log diz POR QUÊ. A causa raiz é a explicação que liga os dois.
Depois de coletar métricas/eventos no Zabbix, SEMPRE tente correlacionar com os logs do Loki:

1. Ache o INSTANTE da anomalia na métrica — use `history_get` / `item_history_summary_get` para
   identificar quando o valor desviou (subiu, zerou, parou de coletar).
2. Chame `loki_query_range` numa janela ao redor desse instante — passe `end_iso` = timestamp do
   incidente e `minutes` cobrindo o desvio (ex: 15). Filtre por erro:
   `|~ "(?i)error|fatal|panic|oom|killed|refused|timeout|denied"`.
3. Monte uma TIMELINE alinhando os sinais no tempo:
   desvio da métrica → linha de log que o explica → disparo da trigger.
4. A `root_cause` é a explicação que conecta os dois sinais. Se os logs NÃO explicarem o desvio
   da métrica, não invente causa: reduza a `confidence`, registre a timeline parcial e...

## FASE [3.5b] DIAGNOSTICAR NO HOST (SSH) — quando Loki não tem logs

Se a fase [3.5] retornar vazio ou logs insuficientes para confirmar a causa raiz, e o host estiver
no inventário (não é zabbix-db), chame `ssh_diagnose` ANTES de escalar:

- Para incidente de **CPU/load alto**: execute `top -bn1 | head -25`, `ps aux --sort=-%cpu | head -20`, `uptime`
- Para incidente de **serviço Zabbix**: execute `journalctl -u zabbix-server --since '30 min ago' --no-pager -n 100` e `sudo tail -n 100 /var/log/zabbix/zabbix_server.log`
- Para qualquer incidente no host: `journalctl -p err --since '30 min ago' --no-pager -n 100`
- Para memória: `free -h`, `ps aux --sort=-%mem | head -20`

A saída do ssh_diagnose é **evidência concreta** — inclua os processos/logs relevantes na `correlation_evidence` e use-os para determinar a `root_cause`. Só então passe para a fase [3.6] ou escalação.

Mapeamento host → fonte de log (LogQL):
- container caído no host `ansible` → `{{container="<nome>"}}` (ex: obs-loki, obs-prometheus, obs-grafana, obs-tempo).
- host sem container conhecido → comece amplo: `{{host="<hostname>"}} |~ "(?i)error|fatal"`.
- Se o próprio Loki for o serviço afetado, não dependa só dele — correlacione com a métrica do Zabbix.
{doc_phase}
---

REGRAS ABSOLUTAS:
1. Sempre diagnostique antes de agir — nunca aja por suposição.
2. Ações proibidas no AGENT.md jamais devem ser executadas.
3. Toda ação executada deve ser descrita no relatório final.
4. Se a causa raiz não for identificada com evidência clara, escale para o SRE.
5. Nunca repita a mesma chamada de ferramenta com os mesmos parâmetros — se não encontrou resultado na primeira vez, tente outra abordagem ou conclua.
6. Responda SEMPRE em JSON puro (sem markdown) ao finalizar:

{{
  "diagnosis": "resumo do que foi investigado",
  "root_cause": "causa raiz objetiva que liga log e métrica (ex: OOM no obs-loki por mem_limit baixo)",
  "correlation_evidence": [
    "HH:MM:SS — sinal de métrica (fonte Zabbix)",
    "HH:MM:SS — linha de log que explica (fonte Loki)",
    "HH:MM:SS — disparo da trigger"
  ],
  "confidence": "alta | media | baixa",
  "doc_analysis": {{
    "consulted": true/false,
    "source": "Context7: <biblioteca> ou null",
    "possible_causes": ["causa provável 1", "causa provável 2"],
    "suggested_checks": ["comando read-only para o humano verificar"],
    "references": ["seção/URL da doc"]
  }},
  "action_taken": "o que foi executado ou 'nenhuma'",
  "resolved": true/false,
  "escalated": true/false,
  "escalation_reason": "motivo ou null",
  "suggested_steps": ["passo 1 concreto para o humano tentar", "passo 2", "passo 3"],
  "notification_message": "mensagem formatada para Telegram (inclua a timeline de correlação e, se houve fase 3.6, as possíveis causas e o que verificar)",
  "open_postmortem": true/false,
  "postmortem_reason": "motivo ou null"
}}

Regra: `suggested_steps` deve ter de 2 a 5 passos CONCRETOS (com comandos reais) quando `escalated: true`.
Se `resolved: true`, pode ser vazio `[]`.
Estes passos serão usados para gerar um runbook rascunho para o operador.

Regras do JSON:
- `correlation_evidence` deve ser uma lista de eventos em ordem cronológica. Se não houver logs
  correlacionáveis, inclua só os sinais de métrica e deixe `confidence` em "baixa".
- `root_cause` só pode ter `confidence: alta` se houver evidência de log E de métrica apontando para a mesma causa.
- `doc_analysis.consulted` = true só se você chamou o Context7 na fase 3.6; senão, `consulted: false` e os demais campos vazios/null.
"""


_AGENT_UNAVAILABLE_KEYWORDS = (
    "agent is not available",
    "agente indisponível",
    "agente nao disponivel",
    "zabbix agent is not available",
    "agent not available",
)


def _detect_incident_pattern(payload: dict) -> str:
    """Retorna dica de investigação baseada no padrão do incidente."""
    text = (
        payload.get("problem", "") + " " + payload.get("trigger", "")
    ).lower()

    if any(kw in text for kw in _AGENT_UNAVAILABLE_KEYWORDS):
        return (
            "\n⚠️  PADRÃO DETECTADO: Agente Zabbix indisponível.\n"
            "INSTRUÇÃO: NÃO consulte items ou histórico do host afetado (agente down = sem dados).\n"
            "Fluxo obrigatório: host_get (obter IP) → ssh_execute restart zabbix-agent2 → problem_active_get (confirmar resolução)."
        )

    # Proxmox VE — hipervisor não tem serviço reiniciável, mas SSH diagnose é obrigatório
    if any(kw in text for kw in ("proxmox", "node [pve]", "node pve", "[pve/", "qemu/")):
        return (
            "\n⚠️  PADRÃO DETECTADO: Alerta Proxmox VE (hipervisor).\n"
            "DIAGNÓSTICO OBRIGATÓRIO — execute nesta EXATA ordem antes de escalar:\n"
            "  1. host_get → confirmar IP do host proxmox (192.168.10.254).\n"
            "  2. item_history_summary_get com hostids=[hostid] e search={'key_': 'mem'} → tendência de memória do nó.\n"
            "  3. ssh_diagnose host_ip='192.168.10.254' com comandos:\n"
            "     ['free -h', 'ps aux --sort=-%mem | head -20',\n"
            "      'journalctl -u pvestatd --since \\'30 min ago\\' --no-pager -n 100',\n"
            "      'journalctl -u pvedaemon --since \\'30 min ago\\' --no-pager -n 100']\n"
            "  4. RACIOCÍNIO CROSS-HOST OBRIGATÓRIO: O Proxmox é um hipervisor — se o nó está com memória\n"
            "     alta, a causa está nas VMs que ele hospeda, NÃO no próprio Proxmox.\n"
            "     - Analise a saída do pvestatd e do ps para identificar qual VM (qemu/ID) está consumindo mais.\n"
            "     - O inventário de VMs e seus IPs: docker=192.168.10.112, ansible=192.168.10.104,\n"
            "       mcp-server=192.168.10.210, zabbix-server=192.168.10.202, zabbix-front=192.168.10.203,\n"
            "       zabbix-proxy=192.168.10.204.\n"
            "     - Se a VM identificada estiver no inventário acima, execute ssh_diagnose NESSA VM:\n"
            "       ['free -h',\n"
            "        'ps -eo pid,user,%mem,%cpu,comm --sort=-%mem | head -30',\n"
            "        'ps -eo pid,user,%mem,%cpu,args --sort=-%mem | head -20',\n"
            "        'journalctl -p err --since \\'30 min ago\\' --no-pager -n 50']\n"
            "     - ANÁLISE OBRIGATÓRIA da saída do ps — para cada processo com %MEM > 1.0:\n"
            "       * Liste: nome do processo/aplicação, PID, %MEM, %CPU\n"
            "       * Agrupe processos do mesmo serviço (ex: múltiplos PIDs do vscode-server → soma total)\n"
            "       * Identifique processos com consumo anômalo para o tipo de host\n"
            "       * Calcule o top-3 consumidores com percentual individual e somado\n"
            "     - Inclua no diagnosis uma lista no formato:\n"
            "       '1. vscode-server: X% RAM (3 processos) | 2. containerd: Y% | 3. node: Z%'\n"
            "     - Se a VM identificada NÃO tiver alerta próprio no Zabbix mas estiver consumindo memória\n"
            "       excessiva, isso é uma anomalia — registre como evidência e escale com essa correlação.\n"
            "  5. SÓ ENTÃO escale — com diagnosis preenchido E suggested_steps com ≥3 passos concretos.\n"
            "PROIBIDO: escalar sem ter investigado a VM causadora do alto consumo no hipervisor."
        )

    # Alta utilização de recurso genérico (CPU, memória, disco)
    if any(kw in text for kw in (
        "high memory", "high cpu", "high load", "disk space",
        "over 9", "over 8", "utilization", "uso alto", "alta utilização",
        "load average", "cpu usage", "memory usage", "disk usage",
    )):
        return (
            "\n⚠️  PADRÃO DETECTADO: Alta utilização de recurso.\n"
            "DIAGNÓSTICO OBRIGATÓRIO antes de escalar:\n"
            "  1. item_history_summary_get → tendência do recurso afetado nas últimas horas.\n"
            "  2. loki_query_range → correlacionar com logs de erro no mesmo período.\n"
            "  3. Se Loki retornar vazio: ssh_diagnose no host com comandos relevantes\n"
            "     (free -h / top / ps aux / journalctl) — NÃO pule esta etapa.\n"
            "  4. Popule suggested_steps com ≥3 passos concretos antes de escalar.\n"
            "PROIBIDO: escalar com suggested_steps vazio ou com menos de 2 passos."
        )

    return ""


def build_incident_prompt(payload: dict) -> str:
    hint = _detect_incident_pattern(payload)
    return f"""INCIDENTE DETECTADO — {datetime.now().isoformat()}

Fonte    : {payload.get('source', 'zabbix')}
Host     : {payload.get('host', 'desconhecido')} ({payload.get('ip', 'IP desconhecido')})
Problema : {payload.get('problem', 'sem descrição')}
Severidade: {payload.get('severity', 'desconhecida')}
Trigger  : {payload.get('trigger', 'N/A')}
Event ID : {payload.get('eventid', 'N/A')}
Timestamp: {payload.get('timestamp', datetime.now().isoformat())}
Detalhes : {json.dumps(payload.get('extra', {}), ensure_ascii=False, indent=2)}
{hint}
Execute o fluxo completo definido no AGENT.md e retorne o JSON de resposta ao final.
"""

# ---------------------------------------------------------------------------
# Fallback: gera suggested_steps via LLM se o agente escalou sem popul-los
# ---------------------------------------------------------------------------

def _ensure_suggested_steps(result: dict, payload: dict) -> None:
    """Safety net: se escalou com suggested_steps vazio, gera passos via LLM."""
    if not result.get("escalated"):
        return
    if len(result.get("suggested_steps") or []) >= 2:
        return

    log.warning("suggested_steps vazio após escalação — gerando via fallback LLM")
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            f"Incidente Zabbix escalado para operador humano:\n"
            f"Host: {payload.get('host')} ({payload.get('ip', '')})\n"
            f"Problema: {payload.get('problem')}\n"
            f"Diagnóstico do agente: {result.get('diagnosis', 'não disponível')}\n"
            f"Causa raiz identificada: {result.get('root_cause', 'não determinada')}\n\n"
            f"Gere de 3 a 5 passos concretos de investigação e remediação para o operador SRE.\n"
            f"Inclua comandos reais quando aplicável.\n"
            f"Responda SOMENTE com um JSON array de strings. Exemplo:\n"
            f'["Verificar X com `comando Y`", "Analisar Z via `comando W`", "Se confirmado, executar Q"]'
        )
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions(timeout=30_000),
            ),
        )
        raw = (resp.text or "").strip()
        match = re.search(r'\[[\s\S]+?\]', raw)
        if match:
            steps = json.loads(match.group())
            if isinstance(steps, list) and steps:
                result["suggested_steps"] = steps
                log.info(f"suggested_steps (fallback): {len(steps)} passos gerados")
    except Exception as e:
        log.warning(f"Fallback suggested_steps falhou: {e}")

# ---------------------------------------------------------------------------
# Motor ReAct — Gemini Flash + MCP Zabbix
# ---------------------------------------------------------------------------

def _zabbix_headers() -> dict:
    return {"Authorization": f"Bearer {ZABBIX_MCP_TOKEN}"} if ZABBIX_MCP_TOKEN else {}


def _mcp_servers() -> list[dict]:
    """Servidores MCP ativos. K8s e Context7 entram só se suas URLs estiverem definidas.
    Cada server traz seus próprios headers de auth (cada serviço usa um esquema)."""
    servers = [{
        "name": "zabbix", "url": ZABBIX_MCP_URL,
        "headers": _zabbix_headers(), "allowed": MCP_ALLOWED_TOOLS,
    }]
    if K8S_MCP_URL:
        servers.append({
            "name": "kubernetes", "url": K8S_MCP_URL,
            "headers": {"Authorization": f"Bearer {K8S_MCP_TOKEN}"} if K8S_MCP_TOKEN else {},
            "allowed": None,  # servidor read-only: todas as tools são de leitura
        })
    if CONTEXT7_MCP_URL:
        servers.append({
            "name": "context7", "url": CONTEXT7_MCP_URL,
            # Context7 autentica pela header própria, não por Authorization Bearer.
            "headers": {"CONTEXT7_API_KEY": CONTEXT7_API_KEY} if CONTEXT7_API_KEY else {},
            "allowed": CONTEXT7_ALLOWED_TOOLS,
        })
    return servers


async def _mcp_list_one(url: str, headers: dict, allowed: set | None) -> list:
    """Lista as tools de UM servidor MCP (filtradas por `allowed`, ou todas se None)."""
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            return [t for t in resp.tools if allowed is None or t.name in allowed]


def _collect_tools() -> tuple[list[types.FunctionDeclaration], dict]:
    """
    Lista as tools de todos os MCP servers ativos (Zabbix + K8s + Context7), monta as
    FunctionDeclaration e o mapa de roteamento tool→(url, headers).
    Fail-open: server indisponível é logado e ignorado — o agente segue com os demais.
    """
    decls: list[types.FunctionDeclaration] = []
    route: dict[str, tuple[str, dict]] = {}
    for srv in _mcp_servers():
        try:
            tools = asyncio.run(_mcp_list_one(srv["url"], srv["headers"], srv["allowed"]))
        except Exception as e:
            log.warning(f"MCP {srv['name']} indisponível: {e} — seguindo sem ele")
            continue
        for t in tools:
            route[t.name] = (srv["url"], srv["headers"])
            decls.append(_mcp_tool_to_gemini(t))
        log.info(f"MCP {srv['name']}: {len(tools)} tools expostas ao Gemini")
    decls += [_SSH_TOOL_DECLARATION, _SSH_DIAGNOSE_DECLARATION, _LOKI_TOOL_DECLARATION]
    return decls, route


async def _mcp_call(url: str, headers: dict, name: str, args: dict) -> str:
    """Abre conexão a UM servidor MCP, chama a tool e fecha. Operação rápida e isolada."""
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            return "\n".join(c.text for c in result.content if hasattr(c, "text"))


def _mcp_call_zabbix(name: str, args: dict) -> str:
    """Chamada síncrona ao MCP Zabbix — usada pelo scheduler (persistência + ack)."""
    return asyncio.run(_mcp_call(ZABBIX_MCP_URL, _zabbix_headers(), name, args))


# No SDK google-genai, FunctionCall.args já é um dict puro — não precisa de conversão de proto.

# ---------------------------------------------------------------------------
# Circuit breaker de quota — protege contra rajada de 429 do Gemini
# ---------------------------------------------------------------------------

_breaker_lock = threading.Lock()
_breaker = {"fails": 0, "opened_at": 0.0}


def _is_quota_error(exc: Exception) -> bool:
    """Detecta 429/quota do Gemini de forma robusta (tipo OU mensagem)."""
    if type(exc).__name__ in ("ResourceExhausted", "TooManyRequests"):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("429", "resourceexhausted", "quota", "rate limit"))


def _breaker_is_open() -> bool:
    """True se o circuito está aberto (em cooldown). Fecha sozinho ao expirar."""
    with _breaker_lock:
        if _breaker["opened_at"] == 0.0:
            return False
        if time.time() - _breaker["opened_at"] >= BREAKER_COOLDOWN:
            _breaker["opened_at"] = 0.0
            _breaker["fails"] = 0
            log.info("Circuit breaker fechado — cooldown expirado, retomando chamadas ao Gemini")
            return False
        return True


def _breaker_record_failure() -> None:
    """Conta uma falha de quota; abre o circuito ao atingir o threshold."""
    with _breaker_lock:
        _breaker["fails"] += 1
        if _breaker["fails"] >= BREAKER_THRESHOLD and _breaker["opened_at"] == 0.0:
            _breaker["opened_at"] = time.time()
            log.warning(
                f"Circuit breaker ABERTO — {_breaker['fails']} falhas de quota seguidas; "
                f"pausando chamadas ao Gemini por {BREAKER_COOLDOWN}s"
            )


def _breaker_record_success() -> None:
    """Sucesso zera o contador e fecha o circuito."""
    with _breaker_lock:
        if _breaker["fails"] or _breaker["opened_at"]:
            log.info("Circuit breaker resetado — chamada bem-sucedida")
        _breaker["fails"] = 0
        _breaker["opened_at"] = 0.0


def run_agent(payload: dict) -> dict:
    """
    Loop ReAct síncrono: cada operação MCP abre/fecha sua própria conexão,
    evitando conflito entre o event loop do MCP e as chamadas síncronas do Gemini.
    """
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY não configurada")
        return {"error": "API key ausente", "escalated": True}

    # Circuit breaker aberto → fast-fail sem gastar quota nem tempo.
    if _breaker_is_open():
        log.warning("Circuit breaker aberto — fast-fail sem chamar o Gemini")
        return {
            "diagnosis":            "LLM indisponível (quota Gemini esgotada) — circuit breaker aberto.",
            "escalated":            True,
            "escalation_reason":    "Circuit breaker de quota aberto",
            "resolved":             False,
            "open_postmortem":      False,
            "notification_message": (
                f"🔴 *[ESCALADO — LLM INDISPONÍVEL]*\n"
                f"Quota do Gemini esgotada (circuit breaker aberto).\n"
                f"Incidente em `{payload.get('host')}`: {payload.get('problem')}\n"
                f"Ação necessária: investigação manual."
            ),
            "duration_s": 0.0,
        }

    start = time.time()
    try:
        # 1. Carregar ferramentas de todos os MCP servers + o mapa de roteamento
        declarations, route = _collect_tools()

        # 2. Cliente Gemini com as ferramentas descobertas. O timeout por chamada
        #    vai no http_options — ATENÇÃO: em MILISSEGUNDOS no google-genai.
        client = genai.Client(api_key=GEMINI_API_KEY)
        config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(load_context()),
            tools=[types.Tool(function_declarations=declarations)],
            http_options=types.HttpOptions(timeout=LLM_CALL_TIMEOUT * 1000),
            # Loop ReAct manual: o agente executa as tools, não o SDK.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        chat = client.chats.create(model=GEMINI_MODEL, config=config)

        log.info(f"Iniciando ReAct — problema: {payload.get('problem')} | host: {payload.get('host')}")
        response = chat.send_message(build_incident_prompt(payload))

        # 3. Loop ReAct: cada tool call abre sua própria conexão MCP
        for turn in range(MAX_TURNS):
            # Deadline wall-clock: interrompe investigação que se arrasta.
            if time.time() - start > INCIDENT_DEADLINE:
                raise TimeoutError(
                    f"deadline de incidente ({INCIDENT_DEADLINE}s) excedido no turn {turn + 1}"
                )

            function_calls = response.function_calls or []
            if not function_calls:
                break

            tool_results = []
            for fc in function_calls:
                args = dict(fc.args or {})
                log.info(f"[TURN {turn + 1}] → {fc.name}({args})")
                if fc.name == "ssh_execute":
                    result_text = _ssh_run(args.get("host_ip", ""), args.get("command", ""))
                elif fc.name == "ssh_diagnose":
                    result_text = _ssh_diagnose(args.get("host_ip", ""), args.get("commands", []))
                elif fc.name == "loki_query_range":
                    result_text = _loki_query_range(
                        args.get("query", ""),
                        int(args.get("minutes", 15) or 15),
                        args.get("end_iso", ""),
                        int(args.get("limit", 100) or 100),
                    )
                elif fc.name == "script_execute":
                    # Atuador privilegiado: autorizar em código antes de tocar o MCP.
                    denial = _script_execute_guard(args)
                    if denial:
                        result_text = denial
                    else:
                        url, headers = route.get(fc.name, (ZABBIX_MCP_URL, _zabbix_headers()))
                        result_text = asyncio.run(_mcp_call(url, headers, fc.name, args))
                else:
                    # Roteia a tool para o servidor MCP dono dela (Zabbix, K8s ou Context7).
                    url, headers = route.get(fc.name, (ZABBIX_MCP_URL, _zabbix_headers()))
                    result_text = asyncio.run(_mcp_call(url, headers, fc.name, args))
                log.info(f"[TURN {turn + 1}] ← {fc.name} OK")

                tool_results.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result_text},
                    )
                )

            response = chat.send_message(tool_results)

        # 4. Extrair JSON final da resposta
        raw = (response.text or "").strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]+\}', raw)
            result = json.loads(match.group()) if match else {"raw": raw, "escalated": True}

        # Safety net: garante suggested_steps preenchidos quando escalado
        _ensure_suggested_steps(result, payload)

        # Chegou aqui = Gemini respondeu sem erro de quota → fecha o breaker.
        _breaker_record_success()

    except TimeoutError as e:
        log.warning(f"Deadline excedido: {e}")
        result = {
            "diagnosis":            f"Investigação interrompida: {e}",
            "escalated":            True,
            "escalation_reason":    "Deadline de incidente excedido — investigação parcial",
            "resolved":             False,
            "open_postmortem":      True,
            "postmortem_reason":    "Agente não concluiu o diagnóstico dentro do tempo limite",
            "notification_message": (
                f"🟡 *[ESCALADO — DEADLINE]*\n"
                f"Investigação em `{payload.get('host')}` excedeu {INCIDENT_DEADLINE}s e foi interrompida.\n"
                f"Problema: {payload.get('problem')}\n"
                f"Ação necessária: investigação manual."
            ),
        }

    except Exception as e:
        log.error(f"Erro no agente: {e}")
        if _is_quota_error(e):
            _breaker_record_failure()
        result = {
            "diagnosis":            f"Erro na execução do agente: {e}",
            "escalated":            True,
            "escalation_reason":    "Falha na execução do agente",
            "resolved":             False,
            "open_postmortem":      False,
            "notification_message": (
                f"🔴 *[FALHA DO AGENTE]*\n"
                f"Erro ao processar incidente em `{payload.get('host')}`\n"
                f"Motivo: `{e}`\n"
                f"Ação necessária: verificação manual imediata."
            ),
        }

    result["duration_s"] = round(time.time() - start, 1)
    log.info(
        f"Agente concluído em {result['duration_s']}s | "
        f"resolvido={result.get('resolved')} | escalado={result.get('escalated')}"
    )
    return result

# ---------------------------------------------------------------------------
# Notificação Telegram
# ---------------------------------------------------------------------------

def notify(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram não configurado — verifique TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID")
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Notificação enviada via Telegram")
    except Exception as e:
        log.error(f"Falha ao enviar Telegram: {e}")

# ---------------------------------------------------------------------------
# Registro de incidente
# ---------------------------------------------------------------------------

def log_incident(payload: dict, result: dict) -> None:
    INCIDENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp":   datetime.now().astimezone().isoformat(),
        "host":        payload.get("host", "unknown"),
        "problem":     payload.get("problem", "unknown"),
        "severity":    payload.get("severity", "unknown"),
        "root_cause":  result.get("root_cause", result.get("diagnosis", "unknown")),
        "confidence":  result.get("confidence", "unknown"),
        "action":      result.get("action_taken", "none"),
        "resolved":    result.get("resolved", False),
        "escalated":   result.get("escalated", False),
        "duration_s":  result.get("duration_s", 0),
    }
    with open(INCIDENT_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info(f"Incidente registrado: {record}")


# ---------------------------------------------------------------------------
# Runbooks automáticos — auto-aprendizado
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Converte texto em slug kebab-case para nomes de arquivo."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:50]


def _next_rb_number() -> int:
    """Retorna o próximo número de runbook disponível em docs/runbooks/."""
    RUNBOOKS_PATH.mkdir(parents=True, exist_ok=True)
    existing = [
        int(m.group(1))
        for f in RUNBOOKS_PATH.glob("RB-[0-9]*.md")
        if (m := re.match(r"RB-(\d+)", f.name))
    ]
    return (max(existing) + 1) if existing else 1


def _create_resolved_runbook(payload: dict, result: dict, incident_id: str) -> Path | None:
    """Cria runbook completo após resolução autônoma bem-sucedida."""
    RUNBOOKS_PATH.mkdir(parents=True, exist_ok=True)
    host    = payload.get("host", "unknown")
    trigger = payload.get("trigger", payload.get("problem", "incident"))
    rb_num  = _next_rb_number()
    slug    = f"{_slugify(host)}-{_slugify(trigger)}"
    rb_path = RUNBOOKS_PATH / f"RB-{rb_num:03d}-{slug}.md"

    timeline = result.get("correlation_evidence") or []
    timeline_md = "\n".join(f"- {e}" for e in timeline) if timeline else "- (sem timeline registrada)"

    content = f"""# RB-{rb_num:03d} — {host}: {trigger}

> **Criado em:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
> **Incidente de origem:** `{incident_id}`
> **Gerado por:** Agente autônomo v2.5.0 (resolução autônoma)
> **Status:** ✅ RESOLVIDO AUTONOMAMENTE

---

## Sintomas

- **Trigger:** {trigger}
- **Host:** {host} ({payload.get('ip', 'IP desconhecido')})
- **Severidade:** {payload.get('severity', 'desconhecida')}
- **Problema:** {payload.get('problem', '')}

## Causa Raiz

{result.get('root_cause', 'não determinada')}

**Confiança:** {result.get('confidence', 'desconhecida')}

## Timeline (log × métrica)

{timeline_md}

## Solução Executada pelo Agente

{result.get('action_taken', result.get('action', 'nenhuma ação registrada'))}

## Diagnóstico

{result.get('diagnosis', '')}

## Prevenção

> ⚠️ *Seção a preencher pelo operador após revisão.*

---
*Runbook gerado automaticamente pelo agente após resolução autônoma em {datetime.now().isoformat()}.*
*Revisar antes de usar como referência em produção.*
"""
    rb_path.write_text(content)
    log.info(f"Runbook criado (resolução autônoma): {rb_path}")
    return rb_path


def _create_escalation_draft(payload: dict, result: dict, incident_id: str) -> Path | None:
    """Cria rascunho de runbook para incidente escalado — aguarda resolução humana."""
    POSTMORTEM_DIR.mkdir(parents=True, exist_ok=True)
    host    = payload.get("host", "unknown")
    trigger = payload.get("trigger", payload.get("problem", "incident"))
    draft_path = POSTMORTEM_DIR / f"RB-DRAFT-{incident_id}.md"

    steps = result.get("suggested_steps") or []
    steps_md = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps)) if steps else "*(agente não gerou passos sugeridos)*"

    timeline = result.get("correlation_evidence") or []
    timeline_md = "\n".join(f"- {e}" for e in timeline) if timeline else "- (sem correlação registrada)"

    doc = result.get("doc_analysis") or {}
    doc_md = ""
    if doc.get("consulted"):
        causes = "\n".join(f"- {c}" for c in (doc.get("possible_causes") or []))
        checks = "\n".join(f"- `{c}`" for c in (doc.get("suggested_checks") or []))
        doc_md = f"\n### Documentação (Context7 — {doc.get('source', '')})\n\n**Possíveis causas:**\n{causes}\n\n**O que verificar:**\n{checks}\n"

    content = f"""# RB-DRAFT — {incident_id}: {host}: {trigger}

> **Rascunho gerado pelo agente em:** {datetime.now().strftime('%Y-%m-%d %H:%M')}
> **⚠️ AGUARDANDO RESOLUÇÃO HUMANA**
>
> Após resolver o incidente, ensine o agente:
> ```bash
> python scripts/zabbix_agent.py --teach {incident_id} "como você resolveu"
> ```

---

## Incidente

- **Host:** {host} ({payload.get('ip', '')})
- **Problema:** {payload.get('problem', '')}
- **Trigger:** {trigger}
- **Severidade:** {payload.get('severity', '')}
- **Detectado em:** {payload.get('timestamp', datetime.now().isoformat())}
- **Motivo da escalação:** {result.get('escalation_reason', 'não especificado')}

## Diagnóstico do Agente

{result.get('diagnosis', 'não registrado')}

**Causa raiz (hipótese):** {result.get('root_cause', 'não determinada')}
**Confiança:** {result.get('confidence', 'desconhecida')}

## Evidências Coletadas

### Timeline (log × métrica)

{timeline_md}
{doc_md}
## Passos Sugeridos pelo Agente

{steps_md}

---

## ✏️ RESOLUÇÃO REAL (preencher após resolver)

**O que foi feito:**
> *(a preencher)*

**Causa confirmada:**
> *(a preencher)*

**Tempo de resolução:** *(a preencher)*

---
*Rascunho gerado pelo agente autônomo v2.5.0 em {datetime.now().isoformat()}.*
"""
    draft_path.write_text(content)
    log.info(f"Rascunho de runbook criado: {draft_path}")
    return draft_path


def create_postmortem(payload: dict, diagnosis: str, incident_id: str = "") -> Path | None:
    if not POSTMORTEM_TPL.exists():
        return None
    if not incident_id:
        incident_id = f"INC-{datetime.now().strftime('%Y-%m-%d-%H%M')}"
    dst = POSTMORTEM_DIR / f"{incident_id}.md"
    filled = (
        POSTMORTEM_TPL.read_text()
        .replace("INC-YYYY-MM-DD-001", incident_id)
        .replace("YYYY-MM-DD", datetime.now().strftime("%Y-%m-%d"))
        .replace("_Preencher aqui._", f"*Auto-gerado pelo agente.*\n\n{diagnosis}")
    )
    dst.write_text(filled)
    log.info(f"Postmortem criado: {dst}")
    return dst

# ---------------------------------------------------------------------------
# Pipeline de processamento
# ---------------------------------------------------------------------------

def process_incident(payload: dict) -> None:
    """
    Wrapper resiliente: garante que qualquer falha não tratada no pipeline ainda
    gere uma notificação de escalação. Sem isso, uma exceção em create_postmortem
    ou log_incident morreria silenciosa na thread daemon — depois de o agente já
    ter dado acknowledge no evento, deixando o incidente mascarado no Zabbix.
    """
    try:
        _process_incident(payload)
    except Exception as e:
        log.exception(f"Falha não tratada no pipeline de incidente: {e}")
        notify(
            f"🔴 *[FALHA DO AGENTE]*\n"
            f"Erro não tratado ao processar incidente em `{payload.get('host')}`\n"
            f"Problema: {payload.get('problem')}\n"
            f"Motivo: `{e}`\n"
            f"Ação necessária: verificação manual imediata."
        )


def _doc_analysis_block(doc: dict | None) -> str:
    """Renderiza a análise de documentação (fase 3.6) para o postmortem, se houver."""
    if not doc or not doc.get("consulted"):
        return ""
    causes = doc.get("possible_causes") or []
    checks = doc.get("suggested_checks") or []
    refs   = doc.get("references") or []
    parts = [f"\n\n**Análise de documentação ({doc.get('source', 'Context7')}):**"]
    if causes:
        parts.append("\n\n*Possíveis causas:*\n" + "\n".join(f"- {c}" for c in causes))
    if checks:
        parts.append("\n\n*O que verificar (read-only):*\n" + "\n".join(f"- `{c}`" for c in checks))
    if refs:
        parts.append("\n\n*Referências:*\n" + "\n".join(f"- {r}" for r in refs))
    return "".join(parts)


def _process_incident(payload: dict) -> None:
    # A espera de persistência e o acknowledge já aconteceram no scheduler antes
    # de chegar aqui (ver _on_scheduled_due). O worker só faz a investigação.

    # Cooldown semântico: se (host, trigger) foi investigado recentemente,
    # envia apenas uma notificação curta em vez de rodar todo o loop ReAct.
    recurring = _check_semantic_cooldown(payload)
    if recurring:
        _notify_recurring(payload, recurring)
        _update_semantic_seen(payload, recurring.get("last_conclusion", "recorrente"))
        return

    log.info(f"Processando: {payload.get('problem')} | host: {payload.get('host')}")

    # ID único do incidente — compartilhado entre postmortem, runbook e draft.
    incident_id = f"INC-{datetime.now().strftime('%Y-%m-%d-%H%M')}"

    result = run_agent(payload)

    msg = result.get("notification_message", "")
    if not msg:
        status = "🟢 RESOLVIDO" if result.get("resolved") else "🔴 ESCALADO"
        msg = f"{status} — `{payload.get('host')}`: {payload.get('problem')}\n{result.get('diagnosis', '')}"
    notify(msg)

    if result.get("open_postmortem"):
        timeline = result.get("correlation_evidence") or []
        diag_block = (
            f"**Causa raiz:** {result.get('root_cause', result.get('diagnosis', 'não identificada'))}\n"
            f"**Confiança:** {result.get('confidence', 'desconhecida')}\n\n"
            f"**Diagnóstico:** {result.get('diagnosis', '')}\n\n"
            "**Timeline de correlação (log × métrica):**\n"
            + ("\n".join(f"- {e}" for e in timeline) if timeline else "- (sem correlação registrada)")
            + _doc_analysis_block(result.get("doc_analysis"))
        )
        pf = create_postmortem(payload, diag_block, incident_id)
        if pf:
            notify(f"📋 Postmortem criado: `{pf.name}`")

    # Auto-runbook: resolução autônoma → runbook completo; escalação → rascunho para humano.
    if result.get("resolved"):
        rb = _create_resolved_runbook(payload, result, incident_id)
        if rb:
            notify(f"📚 Runbook criado: `{rb.name}`")
    elif result.get("escalated"):
        draft = _create_escalation_draft(payload, result, incident_id)
        if draft:
            notify(
                f"📝 Rascunho de runbook gerado: `{draft.name}`\n"
                f"Após resolver, ensine o agente:\n"
                f"`python scripts/zabbix_agent.py --teach {incident_id} \"como você resolveu\"`"
            )

    log_incident(payload, result)

    # Registra conclusão no cooldown semântico para suprimir re-investigação de flapping.
    conclusion = (
        f"{result.get('root_cause', result.get('diagnosis', 'não determinada'))} "
        f"(confidence: {result.get('confidence', 'desconhecida')})"
    )
    _update_semantic_seen(payload, conclusion)

# ---------------------------------------------------------------------------
# Fila de trabalho + pool de workers — limita a concorrência
# ---------------------------------------------------------------------------
# Antes, cada webhook criava uma thread daemon sem limite: uma tempestade de
# alertas viraria explosão de threads + estouro de quota do Gemini. Agora os
# incidentes entram numa fila limitada e são consumidos por um pool fixo de
# workers. Fila cheia → o webhook responde 503 (back-pressure honesta).

_WORK_Q: "queue.Queue[dict]" = queue.Queue(maxsize=AGENT_QUEUE_MAX)

# Idempotência: o Zabbix reentrega o mesmo evento; sem dedup, dois run_agent →
# possível ação dupla. `_inflight` = eventids em processamento; `_recent` =
# eventids concluídos há menos de DEDUP_TTL. Ambos sob o mesmo lock.
_dedup_lock = threading.Lock()
_inflight: set[str] = set()
_recent: dict[str, float] = {}

# Cooldown semântico — por (host, trigger), independente do eventid.
# Detecta flapping: mesmo problema abrindo/fechando ao redor do threshold,
# onde cada nova abertura tem um eventid diferente e passaria pelo dedup normal.
_sem_lock = threading.Lock()
_sem_seen: dict[str, dict] = {}


def _is_dedup_candidate(eventid: str) -> bool:
    """Eventos de teste/sem id nunca são deduplicados (sempre processam)."""
    return bool(eventid) and eventid not in ("", "N/A", "99999")


def _prune_recent(now: float) -> None:
    """Remove eventids cujo TTL expirou. Chamado sob _dedup_lock."""
    for eid in [e for e, ts in _recent.items() if now - ts > DEDUP_TTL]:
        del _recent[eid]


def _mark_done(eventid: str) -> None:
    """Worker chama ao terminar: tira de inflight e marca como recente (TTL)."""
    if not _is_dedup_candidate(eventid):
        return
    with _dedup_lock:
        _inflight.discard(eventid)
        _recent[eventid] = time.time()


# ---------------------------------------------------------------------------
# Cooldown semântico — anti-flapping por (host, trigger)
# ---------------------------------------------------------------------------

def _sem_key(payload: dict) -> str | None:
    """Chave semântica host::trigger. None se o payload estiver incompleto."""
    host    = str(payload.get("host", "")).strip()
    trigger = str(payload.get("trigger", "")).strip()
    return f"{host}::{trigger}" if host and trigger else None


def _check_semantic_cooldown(payload: dict) -> dict | None:
    """
    Retorna a entry de recorrência se o par (host, trigger) foi investigado
    dentro de INCIDENT_COOLDOWN segundos. None = deve investigar normalmente.
    Expira e remove a entry quando o cooldown passar.
    """
    key = _sem_key(payload)
    if not key:
        return None
    now = time.time()
    with _sem_lock:
        entry = _sem_seen.get(key)
        if not entry:
            return None
        if now - entry["last_seen"] > INCIDENT_COOLDOWN:
            del _sem_seen[key]
            return None
        return dict(entry)   # cópia — libera o lock antes de usar


def _update_semantic_seen(payload: dict, conclusion: str) -> None:
    """Registra ou atualiza o estado semântico após uma investigação concluída."""
    key = _sem_key(payload)
    if not key:
        return
    now = time.time()
    with _sem_lock:
        entry = _sem_seen.get(key)
        if entry:
            entry["count"]          += 1
            entry["last_seen"]       = now
            entry["last_conclusion"] = conclusion
            entry["last_eventid"]    = str(payload.get("eventid", ""))
        else:
            _sem_seen[key] = {
                "first_seen":      now,
                "last_seen":       now,
                "count":           1,
                "last_conclusion": conclusion,
                "last_eventid":    str(payload.get("eventid", "")),
            }


def _notify_recurring(payload: dict, entry: dict) -> None:
    """Envia notificação curta de incidente recorrente sem re-investigar."""
    host       = payload.get("host", "?")
    problem    = payload.get("problem", payload.get("trigger", "?"))
    count      = entry["count"]
    last_min   = max(1, int((time.time() - entry["last_seen"]) / 60))
    first_min  = max(1, int((time.time() - entry["first_seen"]) / 60))
    conclusion = entry.get("last_conclusion", "N/A")

    tag = "🔴 [PERSISTENTE]" if count >= RECURRING_ESCALATION_AT else "🔁 [RECORRENTE]"
    msg = (
        f"{tag} Mesmo incidente — sem nova investigação\n"
        f"Host: `{host}`\n"
        f"Problema: {problem}\n"
        f"Ocorrências: {count}x nos últimos {first_min}min\n"
        f"Última análise: há {last_min}min\n"
        f"Conclusão anterior: {conclusion}"
    )
    if count >= RECURRING_ESCALATION_AT:
        msg += f"\n\n⚠️ Persiste por {count} ocorrências — intervenção manual recomendada."
    notify(msg)
    log.info(f"[RECORRENTE] {host} | {count}x | cooldown ativo")


def _worker_loop(worker_id: int) -> None:
    """Consome incidentes da fila em série. Concorrência total = nº de workers."""
    log.info(f"Worker {worker_id} iniciado")
    while True:
        payload = _WORK_Q.get()
        eventid = str(payload.get("eventid", "")).strip()
        try:
            process_incident(payload)
        except Exception as e:  # process_incident já é resiliente; isto é o cinto extra
            log.exception(f"Worker {worker_id} — erro inesperado: {e}")
        finally:
            _mark_done(eventid)
            _WORK_Q.task_done()


def start_workers() -> None:
    """Sobe o pool fixo de workers (threads daemon de longa duração)."""
    for i in range(AGENT_WORKERS):
        threading.Thread(target=_worker_loop, args=(i + 1,), daemon=True).start()
    log.info(f"Pool de workers iniciado: {AGENT_WORKERS} workers | fila máx {AGENT_QUEUE_MAX}")


def _enqueue_work(payload: dict) -> bool:
    """Coloca o incidente na fila dos workers. False se cheia. Não faz dedup."""
    try:
        _WORK_Q.put_nowait(payload)
        return True
    except queue.Full:
        return False

# ---------------------------------------------------------------------------
# Scheduler — espera de persistência FORA do pool de workers
# ---------------------------------------------------------------------------
# A confirmação de persistência (~60s) e o acknowledge não devem ocupar um slot
# de worker. Uma thread scheduler dedicada segura os incidentes até a hora,
# confirma persistência + faz o ack, e só então os promove à fila dos workers.

_sched_cv = threading.Condition()
_sched_heap: list = []                 # (run_at, seq, payload)
_sched_seq = itertools.count()


def schedule_after(delay_s: float, payload: dict) -> None:
    """Agenda o incidente para promoção à fila dos workers daqui a delay_s."""
    run_at = time.time() + delay_s
    with _sched_cv:
        heapq.heappush(_sched_heap, (run_at, next(_sched_seq), payload))
        _sched_cv.notify()


def _on_scheduled_due(payload: dict) -> None:
    """Chamado pelo scheduler quando a espera termina: confirma persistência,
    faz o ack e promove o incidente à fila dos workers (ou descarta/escala)."""
    eventid = str(payload.get("eventid", "")).strip()

    try:
        check = _mcp_call_zabbix("problem_active_get", {"eventids": [eventid]})
        still_active = eventid in check
    except Exception as e:
        log.warning(f"Verificação de persistência falhou: {e} — prosseguindo")
        still_active = True

    if not still_active:
        log.info(f"Incidente {eventid} resolvido em < {PERSISTENCE_WAIT}s — descartando")
        _mark_done(eventid)
        return

    try:
        _mcp_call_zabbix("event_acknowledge", {
            "eventids": [eventid],
            "action": 6,
            "message": (
                f"🤖 *Agente autônomo assumiu* — investigando...\n"
                f"Host: {payload.get('host')} | Severidade: {payload.get('severity')}"
            ),
        })
        log.info(f"Acknowledge enviado após {PERSISTENCE_WAIT}s — eventid {eventid}")
    except Exception as e:
        log.warning(f"Acknowledge falhou: {e}")

    if not _enqueue_work(payload):
        log.warning(f"Fila cheia ao promover incidente {eventid} — escalando")
        _mark_done(eventid)
        notify(
            f"🔴 *[ESCALADO — FILA CHEIA]*\n"
            f"Incidente em `{payload.get('host')}` não pôde ser processado (fila saturada).\n"
            f"Problema: {payload.get('problem')}\nAção necessária: investigação manual."
        )


def _scheduler_loop() -> None:
    """Thread única: dorme até o próximo incidente vencer e o processa."""
    log.info("Scheduler de persistência iniciado")
    while True:
        with _sched_cv:
            while not _sched_heap:
                _sched_cv.wait()
            run_at, _, payload = _sched_heap[0]
            now = time.time()
            if run_at > now:
                _sched_cv.wait(timeout=run_at - now)
                continue  # re-checa: o topo do heap pode ter mudado
            heapq.heappop(_sched_heap)
        _on_scheduled_due(payload)


def start_scheduler() -> None:
    threading.Thread(target=_scheduler_loop, daemon=True).start()


def submit_incident(payload: dict) -> str:
    """
    Ponto de entrada do webhook. Retorna:
      "scheduled" — aguardará PERSISTENCE_WAIT antes de ir aos workers (evento real)
      "enqueued"  — foi direto para os workers (evento de teste/sem id)
      "duplicate" — eventid já em processamento ou concluído há < DEDUP_TTL
      "full"      — fila cheia (back-pressure)
    """
    eventid = str(payload.get("eventid", "")).strip()
    if _is_dedup_candidate(eventid):
        with _dedup_lock:
            _prune_recent(time.time())
            if eventid in _inflight or eventid in _recent:
                return "duplicate"
            _inflight.add(eventid)
        # Espera de persistência fora do pool; promoção à fila acontece depois.
        schedule_after(PERSISTENCE_WAIT, payload)
        return "scheduled"

    # Evento de teste/sem id → direto aos workers, sem espera nem dedup.
    return "enqueued" if _enqueue_work(payload) else "full"

# ---------------------------------------------------------------------------
# Servidor webhook
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        # Comparação em tempo constante — evita timing side-channel no secret.
        if WEBHOOK_SECRET and not hmac.compare_digest(
            self.headers.get("X-Webhook-Secret", ""), WEBHOOK_SECRET
        ):
            self.send_response(401)
            self.end_headers()
            log.warning(f"Webhook rejeitado — secret inválido de {self.client_address[0]}")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_response(400)
            self.end_headers()
            log.error("Content-Length inválido no webhook")
            return

        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_response(413)
            self.end_headers()
            log.warning(f"Payload rejeitado — Content-Length {length} (máx {MAX_BODY_BYTES})")
            return

        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            log.error(f"Payload inválido: {body[:200]}")
            return

        status = submit_incident(payload)

        if status == "full":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"status": "queue_full"}')
            log.warning(f"Fila cheia ({AGENT_QUEUE_MAX}) — incidente rejeitado (503): {payload.get('problem')}")
            return

        if status == "duplicate":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "duplicate"}')
            log.info(f"Evento duplicado ignorado — eventid {payload.get('eventid')}: {payload.get('problem')}")
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(f'{{"status": "{status}"}}'.encode())

    def log_message(self, fmt, *args):
        log.info(f"HTTP {self.client_address[0]} — {fmt % args}")


def start_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    start_workers()
    start_scheduler()
    log.info(f"Zabbix Agent iniciado — escutando em 0.0.0.0:{port}")
    log.info(f"Modelo: {GEMINI_MODEL} | MCP: {ZABBIX_MCP_URL}")
    if not WEBHOOK_SECRET:
        log.warning("⚠️  WEBHOOK_SECRET não definido — endpoint SEM autenticação. "
                    "Defina no .env.zabbix-agent antes de expor o serviço.")
    if not ZABBIX_ALLOWED_SCRIPT_IDS:
        log.info("script_execute desabilitado (allowlist vazia) — defina "
                 "ZABBIX_ALLOWED_SCRIPT_IDS para habilitar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Servidor encerrado")

# ---------------------------------------------------------------------------
# Payload de teste
# ---------------------------------------------------------------------------

PAYLOADS_TEST = {
    # Cenário base — container caído (diagnóstico simples)
    "container_down": {
        "source":    "zabbix",
        "host":      "ansible",
        "ip":        "192.168.10.104",
        "problem":   "Container obs-loki caído",
        "trigger":   "Docker: Container obs-loki is not running",
        "severity":  "SEV2",
        "eventid":   "99999",
        "timestamp": datetime.now().isoformat(),
        "extra": {
            "trigger_id": "12345",
            "event_id":   "99999",
            "item":       "docker.container.status[obs-loki]",
        },
    },

    # Cenário de CORRELAÇÃO log × métrica — OOM kill.
    # Força o caminho completo: ver a métrica de memória subir (Zabbix history_get)
    # → buscar o OOMKilled no Loki na mesma janela → montar timeline → root_cause.
    "oom_correlation": {
        "source":    "zabbix",
        "host":      "ansible",
        "ip":        "192.168.10.104",
        "problem":   "Container obs-tempo reiniciou — uso de memória atingiu o limite",
        "trigger":   "Docker: Container obs-tempo restarted (exit code 137)",
        "severity":  "SEV2",
        "eventid":   "99999",
        "timestamp": datetime.now().isoformat(),
        "extra": {
            "trigger_id":   "12346",
            "event_id":     "99999",
            "container":    "obs-tempo",
            "exit_code":    137,
            "mem_item":     "docker.mem.usage[obs-tempo]",
            "mem_limit_mb": 512,
            # Dica de correlação para o agente:
            #   métrica → docker.mem.usage[obs-tempo] subiu até ~512MB (mem_limit)
            #   log     → {container="obs-tempo"} |~ "(?i)oom|killed|out of memory"
            "hint": (
                "Métrica de memória do container bateu no mem_limit (512MB) momentos antes do "
                "exit 137. Correlacione com os logs do Loki do container obs-tempo na janela do "
                "incidente para confirmar OOMKilled antes de concluir a causa raiz."
            ),
        },
    },
}

# Cenário padrão do --mode test
PAYLOAD_TEST = PAYLOADS_TEST["container_down"]

# ---------------------------------------------------------------------------
# Feedback loop — --teach: humano ensina o agente após resolver incidente escalado
# ---------------------------------------------------------------------------

def cmd_teach(incident_id: str, resolution: str) -> None:
    """
    Lê o rascunho do incidente (RB-DRAFT-{incident_id}.md), usa o LLM para sintetizar
    um runbook completo com a resolução do operador, salva como RB-NNN oficial e
    remove o rascunho. Notifica via Telegram.
    """
    # Procura o draft (postmortem dir primeiro, depois runbooks)
    candidates = list(POSTMORTEM_DIR.glob(f"RB-DRAFT-{incident_id}*.md"))
    if not candidates:
        candidates = list(RUNBOOKS_PATH.glob(f"RB-DRAFT-{incident_id}*.md"))
    if not candidates:
        print(f"❌ Rascunho não encontrado para: {incident_id}")
        print(f"   Buscado em: {POSTMORTEM_DIR} e {RUNBOOKS_PATH}")
        print(f"   Rascunhos disponíveis:")
        for d in sorted(POSTMORTEM_DIR.glob("RB-DRAFT-*.md")):
            print(f"     {d.name}")
        sys.exit(1)

    draft_path   = candidates[0]
    draft_text   = draft_path.read_text()

    # Extrai host e trigger do draft para o slug do runbook
    host_m    = re.search(r"\*\*Host:\*\*\s*([\w-]+)", draft_text)
    trigger_m = re.search(r"\*\*Trigger:\*\*\s*(.+)", draft_text)
    host_slug    = _slugify(host_m.group(1) if host_m else "host")
    trigger_slug = _slugify((trigger_m.group(1) if trigger_m else "incident")[:50])

    print(f"📖 Rascunho encontrado: {draft_path.name}")
    print(f"🤖 Sintetizando runbook com o LLM...")

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "Você é um engenheiro SRE sênior. Com base na investigação do agente e na resolução "
        "informada pelo operador, crie um runbook completo em Markdown.\n\n"
        f"INVESTIGAÇÃO DO AGENTE:\n{draft_text[:3000]}\n\n"
        f"RESOLUÇÃO DO OPERADOR:\n{resolution}\n\n"
        "Crie um runbook com as seções abaixo. Use linguagem técnica, comandos concretos "
        "e seja objetivo.\n\n"
        "## Problema\nDescreva os sintomas observáveis e as condições de disparo.\n\n"
        "## Causa Raiz\nCausa confirmada pelo operador (não hipótese).\n\n"
        "## Diagnóstico\nPasso a passo para identificar o problema (comandos e checks).\n\n"
        "## Solução\nPasso a passo exato para resolver. Use comandos concretos.\n\n"
        "## Prevenção\nO que fazer para evitar recorrência.\n\n"
        "## Referências\nDocumentos ou links relevantes (se houver)."
    )

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions(timeout=LLM_CALL_TIMEOUT * 1000)
            ),
        )
        runbook_body = resp.text or ""
    except Exception as e:
        print(f"❌ Erro ao chamar LLM: {e}")
        sys.exit(1)

    rb_num  = _next_rb_number()
    rb_name = f"RB-{rb_num:03d}-{host_slug}-{trigger_slug}.md"
    rb_path = RUNBOOKS_PATH / rb_name

    header = (
        f"# RB-{rb_num:03d} — {rb_name.replace('.md','').replace(f'RB-{rb_num:03d}-','')}\n\n"
        f"> **Criado em:** {datetime.now().strftime('%Y-%m-%d')}\n"
        f"> **Incidente de origem:** `{incident_id}`\n"
        f"> **Gerado por:** Agente autônomo v2.5.0 + operador\n\n---\n\n"
    )
    RUNBOOKS_PATH.mkdir(parents=True, exist_ok=True)
    rb_path.write_text(header + runbook_body)
    draft_path.unlink()

    msg = (
        f"📚 Runbook criado: `{rb_name}`\n"
        f"Incidente: `{incident_id}`\n"
        f"_Obrigado por ensinar o agente!_"
    )
    notify(msg)
    print(f"✅ Runbook criado: {rb_path}")
    print(f"🗑️  Rascunho removido: {draft_path.name}")
    log.info(f"Runbook ensinado criado: {rb_path} (incidente {incident_id})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agente Autônomo de Incidentes Zabbix")
    parser.add_argument("--mode", choices=["server", "test"], default="server")
    parser.add_argument("--port", type=int, default=WEBHOOK_PORT)
    parser.add_argument(
        "--scenario",
        choices=list(PAYLOADS_TEST.keys()),
        default="container_down",
        help="Cenário do payload de teste (apenas no modo test). "
             "'oom_correlation' exercita a correlação log × métrica.",
    )
    parser.add_argument(
        "--teach",
        nargs=2,
        metavar=("INCIDENT_ID", "RESOLUTION"),
        help=(
            "Ensina o agente após resolução humana de incidente escalado. "
            "Ex: --teach INC-2026-06-12-0934 'parei o processo X que estava em loop'"
        ),
    )
    args = parser.parse_args()

    if args.teach:
        cmd_teach(args.teach[0], args.teach[1])
    elif args.mode == "server":
        start_server(args.port)
    elif args.mode == "test":
        log.info(f"Modo teste — simulando incidente Zabbix (cenário: {args.scenario})")
        process_incident(PAYLOADS_TEST[args.scenario])
