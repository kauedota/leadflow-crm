"""
LeadFlow CRM - mini CRM com automações e integrações.

Simula o que a vaga pede, de ponta a ponta:
  1. Webhook recebe lead (ex.: formulário de investimento / landing page)
  2. Higieniza e valida os dados (telefone, e-mail)
  3. Evita duplicidade (mesmo e-mail ou telefone = mesmo lead)
  4. Distribui para um vendedor (round-robin) e cria tarefa com SLA
  5. Robô de SLA: escala tarefas atrasadas
  6. Integração com ERP quando o lead vira GANHO (idempotente, com retry)
  7. Relatório de funil e conversão

Rodar:  python crm_demo.py          (demo completa)
        python crm_demo.py server   (sobe webhook em http://localhost:8000/webhook/lead)
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

ETAPAS = ["NOVO", "CONTATO", "QUALIFICADO", "PROPOSTA", "GANHO", "PERDIDO"]
VENDEDORES = ["Ana", "Bruno", "Carla"]
SLA_MINUTOS = 15

db = sqlite3.connect(":memory:", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE leads (
    id INTEGER PRIMARY KEY, nome TEXT, email TEXT UNIQUE, telefone TEXT UNIQUE,
    origem TEXT, valor_investimento REAL, etapa TEXT DEFAULT 'NOVO',
    responsavel TEXT, criado_em TEXT, erp_id TEXT
);
CREATE TABLE tarefas (
    id INTEGER PRIMARY KEY, lead_id INTEGER, descricao TEXT,
    prazo TEXT, concluida INTEGER DEFAULT 0, escalada INTEGER DEFAULT 0
);
""")


# ---------- 1) Qualidade dos dados ----------
def normaliza_telefone(tel):
    digitos = re.sub(r"\D", "", tel or "")
    if digitos.startswith("55") and len(digitos) > 11:
        digitos = digitos[2:]
    return digitos if len(digitos) in (10, 11) else None


def normaliza_email(email):
    email = (email or "").strip().lower()
    return email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else None


# ---------- 2) Webhook: entrada de lead ----------
def recebe_lead(payload, agora=None):
    """Retorna (status_http, resposta). Idempotente: reenviar o mesmo lead não duplica."""
    agora = agora or datetime.now()
    email = normaliza_email(payload.get("email"))
    tel = normaliza_telefone(payload.get("telefone"))
    if not payload.get("nome") or not (email and tel):
        return 422, {"erro": "dados inválidos", "email_ok": bool(email), "telefone_ok": bool(tel)}

    # deduplicação: e-mail OU telefone já existente
    existente = db.execute(
        "SELECT id FROM leads WHERE email=? OR telefone=?", (email, tel)).fetchone()
    if existente:
        return 200, {"status": "duplicado", "lead_id": existente["id"]}

    total = db.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    responsavel = VENDEDORES[total % len(VENDEDORES)]  # round-robin
    cur = db.execute(
        "INSERT INTO leads (nome,email,telefone,origem,valor_investimento,responsavel,criado_em)"
        " VALUES (?,?,?,?,?,?,?)",
        (payload["nome"].strip().title(), email, tel, payload.get("origem", "desconhecida"),
         float(payload.get("valor", 0)), responsavel, agora.isoformat()))
    lead_id = cur.lastrowid
    db.execute("INSERT INTO tarefas (lead_id,descricao,prazo) VALUES (?,?,?)",
               (lead_id, "Primeiro contato", (agora + timedelta(minutes=SLA_MINUTOS)).isoformat()))
    db.commit()
    return 201, {"status": "criado", "lead_id": lead_id, "responsavel": responsavel}


# ---------- 3) Pipeline ----------
def move_lead(lead_id, etapa, agora=None):
    assert etapa in ETAPAS
    db.execute("UPDATE leads SET etapa=? WHERE id=?", (etapa, lead_id))
    # gatilho: ao contatar, conclui a tarefa de primeiro contato
    if etapa != "NOVO":
        db.execute("UPDATE tarefas SET concluida=1 WHERE lead_id=? AND descricao='Primeiro contato'",
                   (lead_id,))
    db.commit()
    if etapa == "GANHO":
        sincroniza_erp(lead_id)


# ---------- 4) Robô de SLA ----------
def robo_sla(agora):
    atrasadas = db.execute(
        "SELECT t.id, l.nome, l.responsavel FROM tarefas t JOIN leads l ON l.id=t.lead_id"
        " WHERE t.concluida=0 AND t.escalada=0 AND t.prazo < ?", (agora.isoformat(),)).fetchall()
    for t in atrasadas:
        db.execute("UPDATE tarefas SET escalada=1 WHERE id=?", (t["id"],))
        print(f"  [SLA] contato com {t['nome']} atrasado -> avisando gestor (resp.: {t['responsavel']})")
    db.commit()
    return len(atrasadas)


# ---------- 5) Integração com ERP ----------
_falhas_simuladas = {"restantes": 1}  # ERP "cai" na 1ª chamada para demonstrar retry


def erp_api_cria_cliente(dados):
    """Simula a API do ERP. Em produção: requests.post(URL, json=dados, headers=token)."""
    if _falhas_simuladas["restantes"] > 0:
        _falhas_simuladas["restantes"] -= 1
        raise ConnectionError("ERP indisponível")
    return {"erp_id": f"ERP-{dados['lead_id']:05d}"}


def sincroniza_erp(lead_id, tentativas=3):
    lead = db.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if lead["erp_id"]:  # idempotência: já enviado, não envia de novo
        return lead["erp_id"]
    dados = {"lead_id": lead_id, "nome": lead["nome"], "email": lead["email"],
             "telefone": lead["telefone"], "valor": lead["valor_investimento"]}
    for n in range(1, tentativas + 1):
        try:
            erp_id = erp_api_cria_cliente(dados)["erp_id"]
            db.execute("UPDATE leads SET erp_id=? WHERE id=?", (erp_id, lead_id))
            db.commit()
            print(f"  [ERP] {lead['nome']} enviado -> {erp_id} (tentativa {n})")
            return erp_id
        except ConnectionError as e:
            print(f"  [ERP] falha ({e}), tentativa {n}/{tentativas}")
    print("  [ERP] desistiu: ficará numa fila para reprocessar")


# ---------- 6) Relatório ----------
def relatorio_funil():
    print(f"\n{'ETAPA':<13}{'LEADS':>6}   {'VALOR (R$)':>12}")
    total = db.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    for e in ETAPAS:
        r = db.execute("SELECT COUNT(*) c, COALESCE(SUM(valor_investimento),0) v"
                       " FROM leads WHERE etapa=?", (e,)).fetchone()
        print(f"{e:<13}{r['c']:>6}   {r['v']:>12,.2f}")
    ganhos = db.execute("SELECT COUNT(*) c FROM leads WHERE etapa='GANHO'").fetchone()["c"]
    print(f"\nTaxa de conversão (GANHO/total): {ganhos}/{total} = {ganhos / max(total, 1):.0%}")
    print("Por origem:")
    for r in db.execute("SELECT origem, COUNT(*) c, SUM(etapa='GANHO') g FROM leads GROUP BY origem"):
        print(f"  {r['origem']:<12} leads={r['c']}  ganhos={r['g']}")


# ---------- Servidor webhook real (opcional) ----------
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook/lead":
            self.send_response(404); self.end_headers(); return
        corpo = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            status, resp = recebe_lead(json.loads(corpo))
        except json.JSONDecodeError:
            status, resp = 400, {"erro": "JSON inválido"}
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())


# ---------- Demo ----------
def demo():
    t0 = datetime(2026, 10, 16, 9, 0)
    print("== 1. Leads chegando via webhook ==")
    leads = [
        {"nome": "maria souza", "email": "Maria@Email.com ", "telefone": "(11) 98888-7777", "origem": "Google Ads", "valor": 50000},
        {"nome": "joão lima", "email": "joao@email.com", "telefone": "+55 21 97777-6666", "origem": "Instagram", "valor": 20000},
        {"nome": "MARIA SOUZA", "email": "maria@email.com", "telefone": "11988887777", "origem": "Instagram", "valor": 50000},  # duplicado
        {"nome": "pedro", "email": "pedro-sem-arroba", "telefone": "123", "origem": "Google Ads", "valor": 1000},  # inválido
        {"nome": "ana costa", "email": "ana@email.com", "telefone": "31966665555", "origem": "Google Ads", "valor": 100000},
    ]
    for p in leads:
        status, resp = recebe_lead(p, t0)
        print(f"  {p['nome']:<12} -> HTTP {status} {resp}")

    print("\n== 2. Vendedores trabalham o pipeline (Maria e Ana contatadas, João ignorado) ==")
    move_lead(1, "CONTATO"); move_lead(1, "QUALIFICADO"); move_lead(1, "PROPOSTA")
    print("  Fechando Maria (ERP vai falhar 1x para mostrar o retry):")
    move_lead(1, "GANHO")
    move_lead(3, "CONTATO"); move_lead(3, "PERDIDO")  # Ana Costa: contatada, mas desistiu

    print("\n== 3. 20 min depois, o robô de SLA roda ==")
    n = robo_sla(t0 + timedelta(minutes=20))
    print(f"  {n} tarefa(s) escalada(s)")

    print("\n== 4. Reenvio do ERP (idempotente: não duplica) ==")
    print("  ", sincroniza_erp(1))

    relatorio_funil()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "server":
        print("Webhook em http://localhost:8000/webhook/lead")
        HTTPServer(("", 8000), Handler).serve_forever()
    else:
        demo()
