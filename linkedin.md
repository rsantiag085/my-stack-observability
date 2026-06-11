# LinkedIn Post — Agente Autônomo de SRE no HomeLAB

> Referência: post focado no agente autônomo de incidentes (`zabbix_agent.py`).
> Stack base: Proxmox + Zabbix 7.0 + LGTM Stack + Minikube + MCP Servers.

---

## Texto do post

Meu HomeLAB acabou de contratar um estagiário. 🤖

Ele não dorme, não reclama de plantão e leu todos os runbooks antes do primeiro dia. Brincadeiras à parte: o lab agora conta com um agente autônomo de SRE que trata e resolve incidentes sozinho — dentro dos guardrails que eu defini.

E chamo de estagiário de propósito: ele atua nos incidentes básicos, segue procedimento à risca e, quando a situação foge do que sabe fazer com segurança, para e me chama. Confiança se constrói com escopo pequeno — não dando a chave do datacenter pra IA no primeiro dia.

🔄 O fluxo de trabalho dele:

1. O Zabbix dispara um webhook para o agente.
2. Ele espera 60s para confirmar que o incidente persiste (nada de acordar por falso positivo).
3. Reconhece o incidente no Zabbix — "🤖 Agente autônomo assumiu".
4. Investiga a causa raiz com as ferramentas do MCP Server do Zabbix.
5. Age dentro do permitido — em "Zabbix agent is not available", reinicia o serviço via SSH.
6. Confirma que o problema sumiu e me notifica no Telegram com o diagnóstico.

🧠 A lógica de raciocínio:

Ele roda um loop de raciocínio e ação (ReAct): observa o resultado de cada ferramenta, decide o próximo passo e age — até resolver ou concluir que precisa escalar. Conecta ao MCP em tempo de execução e usa as ferramentas via function calling do Gemini Flash 2.5.

O que torna isso confiável não é o modelo — é o contexto. Antes de agir, ele carrega runbooks, inventário de hosts e guardrails, reconhece padrões conhecidos e segue o playbook. Não é IA "adivinhando".

🔒 Segurança, que foi o que mais me preocupou ao deixar uma IA executar comando em servidor: conta de serviço com sudo restrito SÓ ao restart do serviço, allowlist validada por código, host de banco fora do raio de ação e a regra de ouro — na dúvida, pare e escale.

A meta não é substituir o SRE, é tirar do prato o que é repetitivo e bem documentado. Começou estagiário; vai ganhar responsabilidade conforme eu validar cada caso — sempre com o guardrail antes da capacidade.

Repositório público para quem quiser explorar. 👇

#SRE #Observabilidade #IA #AIOps #Zabbix #Grafana #MCP #Gemini #Automação #IncidentResponse #Kubernetes #HomeLab #DevOps #Proxmox #PlataformaDeEngenharia
