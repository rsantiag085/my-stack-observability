#!/usr/bin/env python3
"""
zabbix_agent.py — Agente Autônomo de Incidentes Zabbix
=======================================================
Versão   : 2.2.0
Criado em: 2026-06-10

Guardrails de atuador em CÓDIGO (não só no prompt):
  - ssh_execute: allowlist de host (nega zabbix-db e hosts fora do inventário) + de comando
  - script_execute: allowlist de scriptid (fail-closed via ZABBIX_ALLOWED_SCRIPT_IDS)
  - webhook: secret em tempo constante + cap de tamanho de corpo
  - process_incident resiliente: falha sempre vira notificação de escalação

Fluxo:
  Webhook Zabbix → Gemini Flash → MCP Zabbix server → ferramentas Zabbix → Telegram

O agente conecta ao MCP server em runtime, descobre as ferramentas disponíveis
automaticamente e as expõe para o Gemini via function calling.

Dependências:
  pip install mcp google-generativeai httpx python-dotenv

Uso:
  python zabbix_agent.py --mode server --port 9001
  python zabbix_agent.py --mode test
"""

import argparse
import asyncio
import hmac
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import google.generativeai as genai
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

ZABBIX_MCP_URL     = os.getenv("ZABBIX_MCP_URL", "http://192.168.10.210:8080/mcp")
ZABBIX_MCP_TOKEN   = os.getenv("ZABBIX_MCP_TOKEN", "")

# Loki — fonte de logs para correlação log × métrica (RCA)
LOKI_URL           = os.getenv("LOKI_URL", "http://192.168.10.104:3100")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

WEBHOOK_PORT       = int(os.getenv("WEBHOOK_PORT", "9001"))
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "")

# Concorrência — pool de workers limitado (anti-tempestade de alertas)
AGENT_WORKERS      = int(os.getenv("AGENT_WORKERS", "2"))
AGENT_QUEUE_MAX    = int(os.getenv("AGENT_QUEUE_MAX", "50"))

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

# Comandos SSH permitidos — guardrail de segurança
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

# Hosts onde ssh_execute pode atuar — espelha o inventário do AGENT.md §7 / docs/hosts.md.
# Guardrail em CÓDIGO: o LLM propõe o host_ip, mas só estes são autorizados.
SSH_ALLOWED_HOSTS = {
    "192.168.10.104",  # ansible
    "192.168.10.112",  # docker
    "192.168.10.210",  # mcp-server
    "192.168.10.202",  # zabbix-server
    "192.168.10.203",  # zabbix-front
    "192.168.10.204",  # zabbix-proxy
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
# Conversão de JSON Schema (MCP) → Gemini Schema
# ---------------------------------------------------------------------------

def _json_schema_to_gemini(schema: dict) -> genai.protos.Schema:
    """Converte um JSON Schema dict para genai.protos.Schema."""
    type_map = {
        "string":  genai.protos.Type.STRING,
        "integer": genai.protos.Type.INTEGER,
        "number":  genai.protos.Type.NUMBER,
        "boolean": genai.protos.Type.BOOLEAN,
        "object":  genai.protos.Type.OBJECT,
        "array":   genai.protos.Type.ARRAY,
    }
    schema_type = type_map.get(schema.get("type", "string"), genai.protos.Type.STRING)
    kwargs = {
        "type":        schema_type,
        "description": schema.get("description", ""),
    }
    if schema_type == genai.protos.Type.OBJECT:
        props = schema.get("properties", {})
        if props:
            kwargs["properties"] = {k: _json_schema_to_gemini(v) for k, v in props.items()}
        if "required" in schema:
            kwargs["required"] = schema["required"]
    elif schema_type == genai.protos.Type.ARRAY:
        items = schema.get("items", {"type": "string"})
        kwargs["items"] = _json_schema_to_gemini(items)
    return genai.protos.Schema(**kwargs)


def _mcp_tool_to_gemini(tool) -> genai.protos.FunctionDeclaration:
    """Converte um MCP Tool para genai.protos.FunctionDeclaration."""
    input_schema = tool.inputSchema or {}
    params = _json_schema_to_gemini(input_schema) if input_schema.get("properties") else None
    return genai.protos.FunctionDeclaration(
        name=tool.name,
        description=tool.description or "",
        parameters=params,
    )

# ---------------------------------------------------------------------------
# Execução SSH — para hosts com Zabbix agent indisponível
# ---------------------------------------------------------------------------

import subprocess as _subprocess

_SSH_TOOL_DECLARATION = genai.protos.FunctionDeclaration(
    name="ssh_execute",
    description=(
        "Executa um comando via SSH em um host remoto do inventário. "
        "Use quando o Zabbix agent estiver indisponível e for necessário reiniciar o serviço. "
        "Comandos permitidos: sudo systemctl restart/start/stop/status zabbix-agent2 (ou zabbix-agent)."
    ),
    parameters=genai.protos.Schema(
        type=genai.protos.Type.OBJECT,
        properties={
            "host_ip": genai.protos.Schema(
                type=genai.protos.Type.STRING,
                description="IP do host alvo, ex: 192.168.10.210",
            ),
            "command": genai.protos.Schema(
                type=genai.protos.Type.STRING,
                description="Comando a executar, ex: sudo systemctl restart zabbix-agent2",
            ),
        },
        required=["host_ip", "command"],
    ),
)


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

_LOKI_TOOL_DECLARATION = genai.protos.FunctionDeclaration(
    name="loki_query_range",
    description=(
        "Consulta logs no Loki (LogQL) numa janela de tempo para correlacionar com a métrica "
        "do incidente e descobrir a CAUSA RAIZ. A métrica diz O QUÊ/QUANDO quebrou; o log diz POR QUÊ. "
        "Sempre filtre por container/host e por nível de erro. "
        "Exemplos de query: '{container=\"obs-loki\"} |~ \"(?i)error|fatal|panic|oom\"' "
        "ou '{host=\"ansible\"} | logfmt | level=\"error\"'."
    ),
    parameters=genai.protos.Schema(
        type=genai.protos.Type.OBJECT,
        properties={
            "query": genai.protos.Schema(
                type=genai.protos.Type.STRING,
                description='Expressão LogQL. Ex: {container="obs-loki"} |~ "(?i)error|fatal"',
            ),
            "minutes": genai.protos.Schema(
                type=genai.protos.Type.INTEGER,
                description="Janela de lookback em minutos a partir de end_iso (padrão 15).",
            ),
            "end_iso": genai.protos.Schema(
                type=genai.protos.Type.STRING,
                description="Fim da janela em ISO8601 (padrão: agora). Use o timestamp do incidente para centrar a janela na anomalia.",
            ),
            "limit": genai.protos.Schema(
                type=genai.protos.Type.INTEGER,
                description="Máximo de linhas a retornar (padrão 100).",
            ),
        },
        required=["query"],
    ),
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

def build_system_prompt(context: str) -> str:
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
   da métrica, não invente causa: reduza a `confidence`, registre a timeline parcial e escale.

Mapeamento host → fonte de log (LogQL):
- container caído no host `ansible` → `{{container="<nome>"}}` (ex: obs-loki, obs-prometheus, obs-grafana, obs-tempo).
- host sem container conhecido → comece amplo: `{{host="<hostname>"}} |~ "(?i)error|fatal"`.
- Se o próprio Loki for o serviço afetado, não dependa só dele — correlacione com a métrica do Zabbix.

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
  "action_taken": "o que foi executado ou 'nenhuma'",
  "resolved": true/false,
  "escalated": true/false,
  "escalation_reason": "motivo ou null",
  "notification_message": "mensagem formatada para Telegram (inclua a timeline de correlação)",
  "open_postmortem": true/false,
  "postmortem_reason": "motivo ou null"
}}

Regras do JSON:
- `correlation_evidence` deve ser uma lista de eventos em ordem cronológica. Se não houver logs
  correlacionáveis, inclua só os sinais de métrica e deixe `confidence` em "baixa".
- `root_cause` só pode ter `confidence: alta` se houver evidência de log E de métrica apontando para a mesma causa.
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
# Motor ReAct — Gemini Flash + MCP Zabbix
# ---------------------------------------------------------------------------

async def _mcp_list_tools() -> list[genai.protos.FunctionDeclaration]:
    """Abre conexão MCP, busca ferramentas e fecha. Operação rápida e isolada."""
    headers = {"Authorization": f"Bearer {ZABBIX_MCP_TOKEN}"} if ZABBIX_MCP_TOKEN else {}
    async with streamablehttp_client(ZABBIX_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_response = await session.list_tools()
            filtered = [t for t in tools_response.tools if t.name in MCP_ALLOWED_TOOLS]
            log.info(f"Ferramentas MCP: {len(tools_response.tools)} disponíveis → {len(filtered)} + ssh_execute + loki_query_range expostas ao Gemini")
            return (
                [_mcp_tool_to_gemini(t) for t in filtered]
                + [_SSH_TOOL_DECLARATION, _LOKI_TOOL_DECLARATION]
            )


async def _mcp_call_tool(name: str, args: dict) -> str:
    """Abre conexão MCP, chama a ferramenta e fecha. Operação rápida e isolada."""
    headers = {"Authorization": f"Bearer {ZABBIX_MCP_TOKEN}"} if ZABBIX_MCP_TOKEN else {}
    async with streamablehttp_client(ZABBIX_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            return "\n".join(c.text for c in result.content if hasattr(c, "text"))


def _proto_args_to_dict(value) -> object:
    """Converte recursivamente MapComposite/proto args do Gemini para dict puro."""
    if hasattr(value, "items"):
        return {k: _proto_args_to_dict(v) for k, v in value.items()}
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        return [_proto_args_to_dict(v) for v in value]
    return value


def run_agent(payload: dict) -> dict:
    """
    Loop ReAct síncrono: cada operação MCP abre/fecha sua própria conexão,
    evitando conflito entre o event loop do MCP e as chamadas síncronas do Gemini.
    """
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY não configurada")
        return {"error": "API key ausente", "escalated": True}

    start = time.time()
    try:
        # 1. Carregar ferramentas do MCP (conexão rápida e fechada em seguida)
        declarations = asyncio.run(_mcp_list_tools())

        # 2. Configurar Gemini com as ferramentas descobertas
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            tools=[genai.protos.Tool(function_declarations=declarations)],
            system_instruction=build_system_prompt(load_context()),
        )
        chat = model.start_chat()

        log.info(f"Iniciando ReAct — problema: {payload.get('problem')} | host: {payload.get('host')}")
        response = chat.send_message(build_incident_prompt(payload))

        # 3. Loop ReAct: cada tool call abre sua própria conexão MCP
        for turn in range(MAX_TURNS):
            function_calls = [
                p for p in response.parts
                if hasattr(p, "function_call") and p.function_call.name
            ]
            if not function_calls:
                break

            tool_results = []
            for part in function_calls:
                fc   = part.function_call
                args = _proto_args_to_dict(fc.args)
                log.info(f"[TURN {turn + 1}] → {fc.name}({args})")
                if fc.name == "ssh_execute":
                    result_text = _ssh_run(args.get("host_ip", ""), args.get("command", ""))
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
                    result_text = denial if denial else asyncio.run(_mcp_call_tool(fc.name, args))
                else:
                    result_text = asyncio.run(_mcp_call_tool(fc.name, args))
                log.info(f"[TURN {turn + 1}] ← {fc.name} OK")

                tool_results.append(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fc.name,
                            response={"result": result_text},
                        )
                    )
                )

            response = chat.send_message(tool_results)

        # 4. Extrair JSON final da resposta
        raw = "".join(
            p.text for p in response.parts if hasattr(p, "text") and p.text
        ).strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]+\}', raw)
            result = json.loads(match.group()) if match else {"raw": raw, "escalated": True}

    except Exception as e:
        log.error(f"Erro no agente: {e}")
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
        "timestamp":   datetime.utcnow().isoformat(),
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


def create_postmortem(payload: dict, diagnosis: str) -> Path | None:
    if not POSTMORTEM_TPL.exists():
        return None
    ts  = datetime.now().strftime("%Y-%m-%d-%H%M")
    dst = POSTMORTEM_DIR / f"INC-{ts}.md"
    filled = (
        POSTMORTEM_TPL.read_text()
        .replace("INC-YYYY-MM-DD-001", f"INC-{ts}")
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


def _process_incident(payload: dict) -> None:
    log.info(f"Processando: {payload.get('problem')} | host: {payload.get('host')}")

    eventid = str(payload.get("eventid", ""))
    valid_event = bool(eventid and eventid not in ("", "N/A", "99999"))

    if valid_event:
        # Aguarda 60s para confirmar que o incidente persiste antes de reconhecer
        log.info(f"Aguardando 60s para confirmar persistência — eventid {eventid}")
        time.sleep(60)

        try:
            check = asyncio.run(_mcp_call_tool("problem_active_get", {"eventids": [eventid]}))
            still_active = eventid in check
        except Exception as e:
            log.warning(f"Verificação de persistência falhou: {e} — prosseguindo")
            still_active = True

        if not still_active:
            log.info(f"Incidente {eventid} resolvido em menos de 1min — descartando")
            return

        try:
            asyncio.run(_mcp_call_tool("event_acknowledge", {
                "eventids": [eventid],
                "action": 6,
                "message": (
                    f"🤖 *Agente autônomo assumiu* — investigando...\n"
                    f"Host: {payload.get('host')} | Severidade: {payload.get('severity')}"
                ),
            }))
            log.info(f"Acknowledge enviado após 60s — eventid {eventid}")
        except Exception as e:
            log.warning(f"Acknowledge falhou: {e}")

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
        )
        pf = create_postmortem(payload, diag_block)
        if pf:
            notify(f"📋 Postmortem criado: `{pf.name}`")

    log_incident(payload, result)

# ---------------------------------------------------------------------------
# Fila de trabalho + pool de workers — limita a concorrência
# ---------------------------------------------------------------------------
# Antes, cada webhook criava uma thread daemon sem limite: uma tempestade de
# alertas viraria explosão de threads + estouro de quota do Gemini. Agora os
# incidentes entram numa fila limitada e são consumidos por um pool fixo de
# workers. Fila cheia → o webhook responde 503 (back-pressure honesta).

_WORK_Q: "queue.Queue[dict]" = queue.Queue(maxsize=AGENT_QUEUE_MAX)


def _worker_loop(worker_id: int) -> None:
    """Consome incidentes da fila em série. Concorrência total = nº de workers."""
    log.info(f"Worker {worker_id} iniciado")
    while True:
        payload = _WORK_Q.get()
        try:
            process_incident(payload)
        except Exception as e:  # process_incident já é resiliente; isto é o cinto extra
            log.exception(f"Worker {worker_id} — erro inesperado: {e}")
        finally:
            _WORK_Q.task_done()


def start_workers() -> None:
    """Sobe o pool fixo de workers (threads daemon de longa duração)."""
    for i in range(AGENT_WORKERS):
        threading.Thread(target=_worker_loop, args=(i + 1,), daemon=True).start()
    log.info(f"Pool de workers iniciado: {AGENT_WORKERS} workers | fila máx {AGENT_QUEUE_MAX}")


def enqueue_incident(payload: dict) -> bool:
    """Enfileira um incidente. Retorna False se a fila estiver cheia (back-pressure)."""
    try:
        _WORK_Q.put_nowait(payload)
        return True
    except queue.Full:
        return False

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

        if not enqueue_incident(payload):
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"status": "queue_full"}')
            log.warning(f"Fila cheia ({AGENT_QUEUE_MAX}) — incidente rejeitado (503): {payload.get('problem')}")
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "accepted"}')

    def log_message(self, fmt, *args):
        log.info(f"HTTP {self.client_address[0]} — {fmt % args}")


def start_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    start_workers()
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
    args = parser.parse_args()

    if args.mode == "server":
        start_server(args.port)
    elif args.mode == "test":
        log.info(f"Modo teste — simulando incidente Zabbix (cenário: {args.scenario})")
        process_incident(PAYLOADS_TEST[args.scenario])
