# SECURITY.md — Política de Segurança do Lab

> Este documento define as práticas de segurança obrigatórias para este laboratório.
> Qualquer agente (humano ou IA) deve ler este arquivo antes de executar `git push` ou criar PRs.

---

## Escopo

Este laboratório roda em ambiente **HomeLAB Proxmox isolado** (`192.168.10.0/24`), sem dados de produção ou acesso externo.
O nível de risco é baixo, mas as boas práticas são mantidas para fins de aprendizado e reprodutibilidade.

**Hosts envolvidos:**

| VM            | IP               | Exposição        |
|---------------|------------------|------------------|
| ansible       | 192.168.10.104   | Stack de obs.    |
| docker        | 192.168.10.112   | Minikube         |
| zabbix-*      | 192.168.10.201–204 | Monitoramento  |
| mcp-server    | 192.168.10.210   | MCP / Zabbix     |

---

## Checklist obrigatório pré-push

Antes de qualquer `git push` ou `git commit` final, verificar:

- [ ] `.env` está no `.gitignore` e **não** está staged (`git status` não deve listá-lo)
- [ ] `.env.ssh` está no `.gitignore` e **não** está staged
- [ ] `.env.mcp-server` está no `.gitignore` e **não** está staged
- [ ] `.env.agent` está no `.gitignore` e **não** está staged
- [ ] `.env.zabbix-agent` está no `.gitignore` e **não** está staged (contém API key do Gemini e webhook secret)
- [ ] Nenhuma senha, token ou API key em texto plano nos arquivos modificados
- [ ] `.env.example`, `.env.mcp-server.example`, `.env.agent.example` e `.env.zabbix-agent.example` contêm **apenas** placeholders (`CHANGE_ME_...`)
- [ ] Nenhuma chave privada (`.pem`, `.key`, `.crt`) nos arquivos staged
- [ ] Nenhum comentário com credenciais (ex: `# senha antiga: abc123`)
- [ ] Nenhuma porta interna exposta indevidamente no `docker-compose.yml`
- [ ] Nenhum IP interno do lab em texto livre em arquivos que não sejam `docs/hosts.md`, `README.md` ou documentação
- [ ] Chave SSH `homelab_ed25519` não está staged (privada, nunca commitar)

---

## Regras permanentes

1. **Segredos ficam apenas nos arquivos `.env*` sem `.example`** (`.env`, `.env.ssh`, `.env.mcp-server`, `.env.agent`, `.env.zabbix-agent`) — nunca em YAMLs, scripts ou código versionado.
2. **`.env.example` é o único arquivo de referência de variáveis** — sempre com placeholders.
3. **Senhas geradas por agentes de IA** devem usar apenas placeholders (`CHANGE_ME_...`).
4. **`privileged: true`** não está em uso na stack atual — qualquer uso futuro exige justificativa documentada no `CHANGELOG.md`.
5. **A API do Prometheus** não deve ser exposta publicamente, mesmo em lab.
6. **Credenciais SSH das VMs** não devem ser commitadas em nenhum arquivo do repositório.
7. **Conta de serviço `svc-zabbix`** (usada pelo agente autônomo para restart via SSH) deve manter o sudo **restrito ao allowlist** (`systemctl {start,stop,restart,status} zabbix-agent2`) via `/etc/sudoers.d/svc-zabbix`. Nunca conceder `NOPASSWD: ALL`. A chave pública autorizada é a `homelab_ed25519` da workstation; a chave privada nunca é commitada.

---

## Conta de serviço `svc-zabbix`

O agente autônomo (`scripts/zabbix_agent.py`) executa restart do `zabbix-agent2` via SSH usando uma conta de serviço dedicada, presente nas 6 VMs do inventário (104, 112, 210, 202, 203, 204).

**Princípios de segurança aplicados:**

- **Menor privilégio:** sudo NOPASSWD restrito apenas aos comandos `systemctl {start,stop,restart,status} zabbix-agent2` (e `zabbix-agent`), via `/etc/sudoers.d/svc-zabbix` (chmod 440). Não pode executar nenhum outro comando privilegiado.
- **Autenticação por chave:** sem senha; acesso somente pela chave `homelab_ed25519` da workstation. `authorized_keys` em chmod 600, `.ssh` em chmod 700.
- **Guardrail em código:** o `zabbix_agent.py` valida o comando contra uma allowlist (`SSH_ALLOWED_COMMANDS`) antes de executar — comando fora da lista é rejeitado sem chegar ao SSH.
- **Escopo do webhook:** o endpoint `:9001` aceita apenas POSTs com o header `X-Webhook-Secret` correto (`WEBHOOK_SECRET` no `.env.zabbix-agent`, mínimo 32 chars).

---

## Geração de segredos

Para gerar valores reais para o `.env`:

```bash
# GF_SECURITY_ADMIN_PASSWORD (mínimo 16 chars)
openssl rand -base64 16

# GF_SECURITY_SECRET_KEY (mínimo 32 chars)
openssl rand -base64 32
```

---

## Histórico de revisões

| Data       | Descrição                                              | Autor |
|------------|--------------------------------------------------------|-------|
| 2026-05-24 | Criação do documento                                   | SRE   |
| 2026-05-25 | Atualização: escopo expandido para HomeLAB Proxmox; checklist revisado (removida referência ao nginx/certs); adicionada regra sobre IPs e credenciais SSH | SRE   |
| 2026-05-25 | Checklist expandido: `.env.ssh` e `.env.mcp-server` adicionados ao `.gitignore` e ao checklist pré-push; regra sobre chave SSH `homelab_ed25519` adicionada | SRE   |
| 2026-05-27 | Referência de `hosts.md` atualizada para `docs/hosts.md` após reorganização de diretórios | SRE   |
| 2026-05-29 | `.env.agent` adicionado ao checklist pré-push e às regras permanentes; `.env.agent.example` incluído na verificação de placeholders | SRE   |
| 2026-06-10 | `.env.zabbix-agent` adicionado ao checklist e às regras; nova seção sobre a conta de serviço `svc-zabbix` (sudo NOPASSWD restrito, auth por chave, guardrail em código, webhook secret) | SRE   |
| 2026-06-11 | Rodada de hardening: permissões dos `.env*` e `logs/zabbix-agent.log` corrigidas para `600` (deixaram de ser world-readable); 26 ocorrências do token Telegram redigidas do log (`bot<REDACTED>`); `httpx` silenciado em INFO para evitar futuros vazamentos de token na URL; `CHANGELOG.md` sanitizado de senhas e tokens; CLAUDE.md e AGENT.md revisados para garantir cobertura dos guardrails de segredo | SRE   |
| 2026-06-12 | Agente v2.4.0: migração para `google-genai` (SDK sem logs de credencial por padrão); guardrails de `ssh_execute` (allowlist de host + comando em código) e `script_execute` (fail-closed via `ZABBIX_ALLOWED_SCRIPT_IDS`) documentados neste arquivo; repositório recriado do zero após detecção de senha no histórico (histórico antigo descartado) | SRE   |
