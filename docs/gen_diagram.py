"""
Gera homelab-diagram.png — ASCII art em fonte matricial, fundo preto.
"""
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/adobe-source-code-pro-fonts/SourceCodePro-Regular.otf"
FONT_SIZE   = 19
BG          = "#000000"
FG          = "#e0e0e0"
PAD         = 32          # margem interna em pixels

DIAGRAM = """\
 ╔══════════════════════════════════════════════════════════════════════════════════════════════════╗
 ║         H O M E L A B   ·   P R O X M O X   ·   1 9 2 . 1 6 8 . 1 0 . 0 / 2 4               ║
 ╠══════════════════════════════════════════════════════════════════════════════════════════════════╣
 ║                                                                                                ║
 ║  ┌──────────────────────┐  ┌─────────────────────────────────────────────────────────────────┐ ║
 ║  │  STACK ZABBIX        │  │  VM ansible  ·  192.168.10.104  ·  Stack de Observabilidade     │ ║
 ║  │  .201 – .204         │  │                                                                 │ ║
 ║  ├──────────────────────┤  │  ┌───────────────────────────────────────────────────────────┐  │ ║
 ║  │                      │  │  │  Grafana 13   :3000                                       │  │ ║
 ║  │  zabbix-proxy  .204  │  │  │  datasources: Prometheus (PromQL) · Loki (LogQL)          │  │ ║
 ║  │         :10051       │  │  │               Tempo (TraceQL)                             │  │ ║
 ║  │           │          │  │  │  dashboards:  kubernetes-overview · logs · graphviz       │  │ ║
 ║  │           ▼          │  │  └─────────────┬────────────────┬──────────────┬─────────────┘  │ ║
 ║  │  zabbix-server .202  │  │                │                │              │                 │ ║
 ║  │         :10051       │  │                ▼                ▼              ▼                 │ ║
 ║  │           │          │  │          Prometheus         Loki 3.0       Tempo 2.4             │ ║
 ║  │           ▼          │  │          :9090              :3100          :3200                 │ ║
 ║  │  zabbix-front  .203  │  │               ▲                ▲              ▲                 │ ║
 ║  │         :80          │  │               └────────────────┴──────────────┘                 │ ║
 ║  │           │          │  │                        OTEL Collector                           │ ║
 ║  │           ▼          │  │                        :4317 gRPC  ·  :4318 HTTP                │ ║
 ║  │  zabbix-db     .201  │  │                        ◄── apps K8s enviam OTLP push            │ ║
 ║  │    PostgreSQL  :5432 │  │                                                                 │ ║
 ║  │                      │  │  Promtail  :9080                                                │ ║
 ║  │  ┌────────────────┐  │  │  Docker socket ─────────────────────────────────────► Loki      │ ║
 ║  │  │ Zabbix Agent 2 │  │  └─────────────────────────────────────────────────────────────────┘ ║
 ║  │  │  todas as VMs  │  │                                                                    ║
 ║  │  │    :10050      │  │  ┌──────────────────────────┐  ┌─────────────────────────────────┐  ║
 ║  │  └────────────────┘  │  │  VM docker  ·  .112      │  │  VM mcp-server  ·  .210         │  ║
 ║  └──────────────────────┘  │  Kubernetes (Minikube)   │  ├─────────────────────────────────┤  ║
 ║                             │                         │  │  Zabbix MCP Server  :8080       │  ║
 ║  ┌──────────────────────┐  │  Minikube cluster        │  │  237 ferramentas                │  ║
 ║  │  AGENTE AUTÔNOMO SRE │  │  ├─ painel-estudos-sre   │  │                                 │  ║
 ║  ├──────────────────────┤  │  │    port-forward :8080  │  │  Kubernetes MCP Server  :8081   │  ║
 ║  │                      │  │  └─ kube-state-metrics    │  │  read-only                      │  ║
 ║  │  Zabbix webhook      │  │       port-forward :32080 │  │                                 │  ║
 ║  │     │                │  │       │                   │  │  Zabbix Agent 2  :10050         │  ║
 ║  │     ▼                │  │       └──► Prometheus     │  └─────────────────────────────────┘  ║
 ║  │  agent_orchestrator  │  │            scrape :32080  │                                    ║
 ║  │     │                │  │                         │                                    ║
 ║  │     ▼                │  │  systemd services:      │                                    ║
 ║  │  Claude API          │  │  minikube-start         │                                    ║
 ║  │     │                │  │  port-forward           │                                    ║
 ║  │     ├──► MCP Zabbix  │  │  DNAT apiserver :8443   │                                    ║
 ║  │     └──► MCP K8s     │  └──────────────────────────┘                                    ║
 ║  │     │                │                                                                  ║
 ║  │     ▼                │                                                                  ║
 ║  │  Telegram            │                                                                  ║
 ║  │  Google Chat         │                                                                  ║
 ║  │                      │                                                                  ║
 ║  │  AGENT.md v1.0       │                                                                  ║
 ║  │  lê/ack  → autônomo  │                                                                  ║
 ║  │  destrutivo → humano │                                                                  ║
 ║  └──────────────────────┘                                                                  ║
 ║                                                                                            ║
 ╚══════════════════════════════════════════════════════════════════════════════════════════════╝"""

def main():
    font = ImageFont.truetype(FONT, FONT_SIZE)

    # medir largura e altura em pixels
    lines = DIAGRAM.split("\n")
    dummy = Image.new("RGB", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    bbox  = draw.textbbox((0, 0), "W", font=font)
    cw    = bbox[2] - bbox[0]   # largura de um caractere
    ch    = bbox[3] - bbox[1]   # altura de um caractere
    line_h = int(ch * 1.25)     # espaçamento entre linhas

    max_chars = max(len(l) for l in lines)
    img_w = max_chars * cw + PAD * 2
    img_h = len(lines) * line_h + PAD * 2

    img  = Image.new("RGB", (img_w, img_h), BG)
    draw = ImageDraw.Draw(img)

    for i, line in enumerate(lines):
        y = PAD + i * line_h
        draw.text((PAD, y), line, font=font, fill=FG)

    out = "/home/robson/projetos/my-stack-observability/docs/homelab-diagram.png"
    img.save(out)
    print(f"Salvo: {out}  ({img_w}x{img_h} px)")

main()
