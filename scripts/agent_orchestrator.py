#!/usr/bin/env python3
"""
agent_orchestrator.py — Orquestrador do Agente Autônomo de Incidentes
======================================================================
Repositório : my-stack-observability
Versão      : 1.0.0
Criado em   : 2026-05-29

Fluxo:
  Webhook Zabbix/K8s → este script → Claude API (com MCP tools) → Slack/Telegram/Google Chat

Dependências:
  pip install anthropic httpx python-dotenv

Uso:
  # Servidor HTTP (recebe webhooks)
  python agent_orchestrator.py --mode server --port 9000

  # Teste manual com payload simulado
  python agent_orchestrator.py --mode test --source zabbix
  python agent_orchestrator.py --mode test --source kubernetes
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuração inicial
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent.parent / ".env.agent")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/agent-orchestrator.log"),
    ],
)
log = logging.getLogger("agent")

# httpx loga a URL completa de cada request em nível INFO — incluindo o token
# do bot Telegram embutido no path. Silenciar para nunca vazar segredo no log.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuração via variáveis de ambiente
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
MODEL               = "claude-sonnet-4-20250514"
MAX_TOKENS          = 4096

# MCP Servers
ZABBIX_MCP_URL      = os.getenv("ZABBIX_MCP_URL",  "http://192.168.10.210:8080/mcp")
K8S_MCP_URL         = os.getenv("K8S_MCP_URL",     "http://192.168.10.210:8081/mcp")
ZABBIX_MCP_TOKEN    = os.getenv("ZABBIX_MCP_TOKEN", "")

# Canais de notificação
NOTIFICATION_CHANNEL   = os.getenv("NOTIFICATION_CHANNEL", "telegram")  # telegram | google_chat | both
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "")
GOOGLE_CHAT_WEBHOOK    = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "")

# Servidor webhook
WEBHOOK_PORT        = int(os.getenv("WEBHOOK_PORT", "9000"))
WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "")

# Paths
AGENT_MD_PATH       = Path(__file__).parent.parent / "AGENT.md"
HOSTS_MD_PATH       = Path(__file__).parent.parent / "docs" / "hosts.md"
RUNBOOKS_PATH       = Path(__file__).parent.parent / "docs" / "runbooks"
POSTMORTEM_TPL      = Path(__file__).parent.parent / "docs" / "postmortem" / "postmortem.md"
POSTMORTEM_DIR      = Path(__file__).parent.parent / "docs" / "postmortem"
INCIDENT_LOG        = Path(__file__).parent.parent / "docs" / "postmortem" / "incident-log.jsonl"

# ---------------------------------------------------------------------------
# Carregamento de contexto
# ---------------------------------------------------------------------------

def load_context() -> str:
    """Carrega AGENT.md, hosts.md e lista de runbooks disponíveis."""
    parts = []

    if AGENT_MD_PATH.exists():
        parts.append(f"# AGENT.md\n{AGENT_MD_PATH.read_text()}")

    if HOSTS_MD_PATH.exists():
        parts.append(f"# hosts.md\n{HOSTS_MD_PATH.read_text()}")

    if RUNBOOKS_PATH.exists():
        runbooks = [f.name for f in sorted(RUNBOOKS_PATH.glob("RB-*.md"))]
        if runbooks:
            parts.append(f"# Runbooks disponíveis\n" + "\n".join(f"- {r}" for r in runbooks))

    return "\n\n---\n\n".join(parts)


def load_runbook(name: str) -> str | None:
    """Carrega o conteúdo de um runbook específico pelo nome parcial."""
    if not RUNBOOKS_PATH.exists():
        return None
    matches = list(RUNBOOKS_PATH.glob(f"*{name}*"))
    if matches:
        return matches[0].read_text()
    return None

# ---------------------------------------------------------------------------
# Notificações
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> bool:
    """Envia mensagem para o grupo Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram não configurado — verifique TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = httpx.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }, timeout=10)
        resp.raise_for_status()
        log.info("Notificação enviada via Telegram")
        return True
    except Exception as e:
        log.error(f"Falha ao enviar Telegram: {e}")
        return False


def _send_google_chat(message: str) -> bool:
    """Envia mensagem para o espaço Google Chat via Incoming Webhook."""
    if not GOOGLE_CHAT_WEBHOOK:
        log.warning("Google Chat não configurado — verifique GOOGLE_CHAT_WEBHOOK_URL")
        return False
    try:
        resp = httpx.post(GOOGLE_CHAT_WEBHOOK, json={"text": message}, timeout=10)
        resp.raise_for_status()
        log.info("Notificação enviada via Google Chat")
        return True
    except Exception as e:
        log.error(f"Falha ao enviar Google Chat: {e}")
        return False


def notify(message: str) -> None:
    """Despacha a notificação para o(s) canal(is) configurado(s)."""
    ch = NOTIFICATION_CHANNEL.lower()
    if ch in ("telegram", "both"):
        _send_telegram(message)
    if ch in ("google_chat", "both"):
        _send_google_chat(message)
    if ch not in ("telegram", "google_chat", "both"):
        log.warning(f"Canal desconhecido: {ch} — verifique NOTIFICATION_CHANNEL no .env.agent")

# ---------------------------------------------------------------------------
# Registro de incidente
# ---------------------------------------------------------------------------

def log_incident(payload: dict, result: dict) -> None:
    """Persiste o registro do incidente em JSONL para rastreabilidade."""
    INCIDENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "source":    payload.get("source", "unknown"),
        "host":      payload.get("host", "unknown"),
        "problem":   payload.get("problem", "unknown"),
        "severity":  payload.get("severity", "unknown"),
        "action":    result.get("action", "none"),
        "resolved":  result.get("resolved", False),
        "escalated": result.get("escalated", False),
        "duration_s": result.get("duration_s", 0),
    }
    with open(INCIDENT_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log.info(f"Incidente registrado: {record}")


def create_postmortem(payload: dict, diagnosis: str) -> Path | None:
    """Cria postmortem a partir do template quando os critérios são atendidos."""
    if not POSTMORTEM_TPL.exists():
        log.warning("Template de postmortem não encontrado")
        return None
    ts  = datetime.now().strftime("%Y-%m-%d-%H%M")
    dst = POSTMORTEM_DIR / f"INC-{ts}.md"
    tpl = POSTMORTEM_TPL.read_text()
    filled = tpl.replace(
        "INC-YYYY-MM-DD-001", f"INC-{ts}"
    ).replace(
        "YYYY-MM-DD", datetime.now().strftime("%Y-%m-%d")
    ).replace(
        "_Preencher aqui._", f"*Auto-gerado pelo agente.*\n\n{diagnosis}"
    )
    dst.write_text(filled)
    log.info(f"Postmortem criado: {dst}")
    return dst

# ---------------------------------------------------------------------------
# Motor do agente — Claude API + MCP
# ---------------------------------------------------------------------------

def build_system_prompt(context: str) -> str:
    return f"""Você é um agente autônomo de SRE operando em modo não-supervisionado.
Seu comportamento é definido pelo AGENT.md abaixo. Leia-o integralmente antes de agir.

{context}

REGRAS ABSOLUTAS:
1. Sempre diagnostique antes de agir — nunca aja por suposição.
2. Ações proibidas no AGENT.md jamais devem ser executadas, independente do contexto.
3. Toda ação executada deve ser descrita no relatório final.
4. Se a causa raiz não for identificada com evidência clara, escale para o SRE.
5. Responda SEMPRE em JSON com a estrutura definida abaixo.

FORMATO DE RESPOSTA OBRIGATÓRIO (JSON puro, sem markdown):
{{
  "diagnosis": "descrição da causa raiz identificada",
  "action_taken": "o que foi executado ou 'nenhuma'",
  "resolved": true/false,
  "escalated": true/false,
  "escalation_reason": "motivo se escalated=true ou null",
  "notification_message": "mensagem formatada para o canal de notificação",
  "open_postmortem": true/false,
  "postmortem_reason": "motivo se open_postmortem=true ou null"
}}
"""


def build_incident_prompt(payload: dict) -> str:
    return f"""INCIDENTE DETECTADO — {datetime.now().isoformat()}

Fonte    : {payload.get('source', 'desconhecida')}
Host     : {payload.get('host', 'desconhecido')} ({payload.get('ip', 'IP desconhecido')})
Problema : {payload.get('problem', 'sem descrição')}
Severidade: {payload.get('severity', 'desconhecida')}
Trigger  : {payload.get('trigger', 'N/A')}
Timestamp: {payload.get('timestamp', datetime.now().isoformat())}
Detalhes : {json.dumps(payload.get('extra', {}), ensure_ascii=False, indent=2)}

Execute o fluxo completo definido no AGENT.md:
1. Contextualize o host no inventário
2. Verifique se existe runbook aplicável
3. Diagnostique usando as ferramentas MCP disponíveis
4. Decida e aja dentro dos guardrails
5. Retorne o JSON de resposta obrigatório
"""


def run_agent(payload: dict) -> dict:
    """Executa o agente Claude com as ferramentas MCP para o incidente recebido."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY não configurada")
        return {"error": "API key ausente", "escalated": True}

    start = time.time()
    context = load_context()
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Construir lista de MCP servers
    mcp_servers = [
        {
            "type": "url",
            "url": ZABBIX_MCP_URL,
            "name": "zabbix-mcp",
            **({"authorization_token": ZABBIX_MCP_TOKEN} if ZABBIX_MCP_TOKEN else {}),
        },
        {
            "type": "url",
            "url": K8S_MCP_URL,
            "name": "kubernetes-mcp",
        },
    ]

    log.info(f"Iniciando agente para incidente: {payload.get('problem')} em {payload.get('host')}")

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system_prompt(context),
            messages=[{"role": "user", "content": build_incident_prompt(payload)}],
            mcp_servers=mcp_servers,
            betas=["mcp-client-2025-04-04"],
        )

        # Extrair resposta de texto
        text_blocks = [b.text for b in response.content if hasattr(b, "text")]
        raw = " ".join(text_blocks).strip()

        # Parse do JSON de resposta
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # Tentar extrair JSON de dentro do texto
            import re
            match = re.search(r'\{[\s\S]+\}', raw)
            result = json.loads(match.group()) if match else {"raw": raw}

        result["duration_s"] = round(time.time() - start, 1)
        log.info(f"Agente concluiu em {result['duration_s']}s | resolvido={result.get('resolved')} | escalado={result.get('escalated')}")
        return result

    except anthropic.APIError as e:
        log.error(f"Erro na API Anthropic: {e}")
        return {
            "diagnosis": f"Erro na API do agente: {e}",
            "escalated": True,
            "escalation_reason": "Falha na execução do agente",
            "resolved": False,
            "open_postmortem": False,
            "notification_message": f"🔴 *[FALHA DO AGENTE]*\nErro ao processar incidente em `{payload.get('host')}`\nMotivo: `{e}`\nAção necessária: verificação manual imediata.",
            "duration_s": round(time.time() - start, 1),
        }

# ---------------------------------------------------------------------------
# Pipeline principal de processamento
# ---------------------------------------------------------------------------

def process_incident(payload: dict) -> None:
    """Pipeline completo: agente → notificação → registro → postmortem."""
    log.info(f"Processando incidente: {payload}")

    # Executar agente
    result = run_agent(payload)

    # Notificar canal configurado
    msg = result.get("notification_message", "")
    if msg:
        notify(msg)
    else:
        # Fallback se o agente não gerou mensagem formatada
        status = "🟢 RESOLVIDO" if result.get("resolved") else "🔴 ESCALADO"
        notify(f"{status} — `{payload.get('host')}`: {payload.get('problem')}\n{result.get('diagnosis', '')}")

    # Abrir postmortem se necessário
    if result.get("open_postmortem"):
        pf = create_postmortem(payload, result.get("diagnosis", ""))
        if pf:
            notify(f"📋 Postmortem criado automaticamente: `{pf.name}`")

    # Registrar no log de incidentes
    log_incident(payload, result)

# ---------------------------------------------------------------------------
# Servidor HTTP — recebe webhooks
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        # Validar secret se configurado
        if WEBHOOK_SECRET:
            auth = self.headers.get("X-Webhook-Secret", "")
            if auth != WEBHOOK_SECRET:
                self.send_response(401)
                self.end_headers()
                log.warning(f"Webhook rejeitado — secret inválido de {self.client_address[0]}")
                return

        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            log.error(f"Payload inválido recebido: {body[:200]}")
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "accepted"}')

        # Processar em background (não bloqueia o webhook)
        import threading
        threading.Thread(target=process_incident, args=(payload,), daemon=True).start()

    def log_message(self, fmt, *args):
        log.info(f"HTTP {self.client_address[0]} — {fmt % args}")


def start_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    log.info(f"Agente orquestrador iniciado — escutando em 0.0.0.0:{port}")
    log.info(f"Canal de notificação: {NOTIFICATION_CHANNEL}")
    log.info(f"Zabbix MCP: {ZABBIX_MCP_URL}")
    log.info(f"Kubernetes MCP: {K8S_MCP_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Servidor encerrado")

# ---------------------------------------------------------------------------
# Payloads de teste
# ---------------------------------------------------------------------------

PAYLOADS_TEST = {
    "zabbix": {
        "source":    "zabbix",
        "host":      "ansible",
        "ip":        "192.168.10.104",
        "problem":   "Container obs-loki caído",
        "trigger":   "Docker: Container obs-loki is not running",
        "severity":  "SEV2",
        "timestamp": datetime.now().isoformat(),
        "extra": {
            "trigger_id": "12345",
            "event_id":   "67890",
            "item":       "docker.container.status[obs-loki]",
        }
    },
    "kubernetes": {
        "source":    "kubernetes",
        "host":      "docker",
        "ip":        "192.168.10.112",
        "problem":   "Pod em CrashLoopBackOff no namespace default",
        "trigger":   "Pod myapp-7d9f8b-xkz2p CrashLoopBackOff",
        "severity":  "SEV2",
        "timestamp": datetime.now().isoformat(),
        "extra": {
            "namespace":   "default",
            "pod":         "myapp-7d9f8b-xkz2p",
            "restart_count": 8,
        }
    },
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agente Autônomo de Incidentes")
    parser.add_argument("--mode",   choices=["server", "test"], default="server")
    parser.add_argument("--port",   type=int, default=WEBHOOK_PORT)
    parser.add_argument("--source", choices=["zabbix", "kubernetes"], default="zabbix",
                        help="Fonte do payload de teste (apenas no modo test)")
    args = parser.parse_args()

    if args.mode == "server":
        start_server(args.port)
    elif args.mode == "test":
        payload = PAYLOADS_TEST[args.source]
        log.info(f"Modo teste — simulando incidente {args.source}")
        process_incident(payload)
