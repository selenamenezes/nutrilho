"""
Demo-only: proposta automática sem depender de cadastros reais de cozinheiro.
Remover quando o fluxo de produção substituir o demo.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from werkzeug.security import generate_password_hash

# --- Identidade fixa (criada no banco na primeira demo, se ainda não existir) ---
DEMO_COZINHEIRO_EMAIL = "demo-cozinheiro-interno@nutrilho.local"
DEMO_COZINHEIRO_NOME = "Marina Costa"
DEMO_COZINHEIRO_TELEFONE = "+5500000000001"

DEMO_COZINHEIRO_ESPECIALIDADE = "Nutrição esportiva e refeições sob medida"
DEMO_COZINHEIRO_NOTA = 4.9
DEMO_COZINHEIRO_RESPOSTA_TEMPO = "Costuma responder em até 2 horas em dias úteis"
DEMO_COZINHEIRO_SOBRE = (
    "Chef há 8 anos no Boa Viagem; prepara exatamente o que você mandou na receita, "
    "ajustando porções e temperos ao seu dia a dia."
)
# Ponto de retirada (cozinha / espaço do cozinheiro)
DEMO_RETIRADA_ENDERECO = "Rua Ernesto de Paula Santos, 187 — Boa Viagem, Recife/PE"

# Preço base do plano (receita + preparo), antes da taxa de entrega
DEMO_BASE_VALOR = Decimal("84.90")

# Texto único para a modal (tempo de preparo)
DEMO_TEMPO_PREPARO_LABEL = (
    "Preparo estimado: 45 a 60 minutos após você confirmar o pedido"
)

# Opções que o cliente escolhe (uma só). taxa em reais; retirada = 0
# entrega_uber: taxa preenchida por solicitação (estimativa simulada)
DEMO_OPCOES_ENTREGA: list[dict[str, Any]] = [
    {"id": "retirada", "label": "Retirada no local", "taxa": 0.0},
    {"id": "entrega_bairro", "label": "Entrega no seu bairro", "taxa": 5.5},
    {"id": "entrega_app", "label": "Entrega via app parceiro (iFood / Rappi)", "taxa": 7.5},
    {"id": "entrega_uber", "label": "Entrega via Uber", "taxa": 0.0},
]


def demo_taxa_uber_estimada(solicitacao_id: int) -> Decimal:
    """Estimativa fictícia de corrida (varia de forma determinística com o id da solicitação)."""
    base = Decimal("8.90")
    delta = Decimal(str((solicitacao_id * 11 + 3) % 25)) * Decimal("0.52")
    return (base + delta).quantize(Decimal("0.01"))


def is_demo_cozinheiro_email(email: str | None) -> bool:
    return (email or "").strip().lower() == DEMO_COZINHEIRO_EMAIL


def demo_taxa_para_opcao(opcao_id: str, solicitacao_id: int | None = None) -> Decimal | None:
    oid = (opcao_id or "").strip()
    if oid == "entrega_uber" and solicitacao_id is not None:
        return demo_taxa_uber_estimada(solicitacao_id)
    for o in DEMO_OPCOES_ENTREGA:
        if o["id"] == oid:
            return Decimal(str(o["taxa"]))
    return None


def demo_valor_total_com_opcao(opcao_id: str, solicitacao_id: int | None = None) -> Decimal | None:
    taxa = demo_taxa_para_opcao(opcao_id, solicitacao_id)
    if taxa is None:
        return None
    return DEMO_BASE_VALOR + taxa


def demo_senha_placeholder_hash() -> str:
    return generate_password_hash("demo-sem-login")


def demo_opciones_json(solicitacao_id: int | None = None) -> list[dict[str, Any]]:
    """Lista serializável em JSON; taxa Uber depende de `solicitacao_id` (estimativa simulada)."""
    out: list[dict[str, Any]] = []
    for x in DEMO_OPCOES_ENTREGA:
        d = dict(x)
        if d["id"] == "entrega_uber":
            if solicitacao_id is not None:
                t = demo_taxa_uber_estimada(solicitacao_id)
                d["taxa"] = float(t)
                d["estimativa"] = True
            else:
                d["taxa"] = 0.0
        out.append(d)
    return out


def demo_proposta_extras_json(cliente: Any | None) -> dict[str, Any]:
    """Metadados do cozinheiro + endereços para a modal e para o cliente escolher entrega."""
    out: dict[str, Any] = {
        'cozinheiro_especialidade': DEMO_COZINHEIRO_ESPECIALIDADE,
        'cozinheiro_nota': DEMO_COZINHEIRO_NOTA,
        'cozinheiro_resposta_tempo': DEMO_COZINHEIRO_RESPOSTA_TEMPO,
        'cozinheiro_sobre': DEMO_COZINHEIRO_SOBRE,
        'tempo_preparo_label': DEMO_TEMPO_PREPARO_LABEL,
        'retirada_endereco': DEMO_RETIRADA_ENDERECO,
        'entrega_endereco_cliente': '',
    }
    if cliente is not None:
        comp = getattr(cliente, 'complemento', None) or ''
        comp_part = f", {comp}" if comp.strip() else ''
        out['entrega_endereco_cliente'] = (
            f"{cliente.rua}, {cliente.numero}{comp_part} — CEP {cliente.cep}"
        )
    return out


def ensure_demo_cozinheiro(db) -> Any:
    """Garante um cozinheiro fixo para FK de Proposta/Pedido (uma linha, criada se faltar)."""
    from views.models import Cozinheiro, Especialidade

    c = db.query(Cozinheiro).filter(Cozinheiro.email == DEMO_COZINHEIRO_EMAIL).first()
    if c:
        ensure_demo_marmita(db, c.id)
        return c
    esp = db.query(Especialidade).first()
    if not esp:
        esp = Especialidade(nome="Demonstração")
        db.add(esp)
        db.flush()
    c = Cozinheiro(
        nome=DEMO_COZINHEIRO_NOME,
        email=DEMO_COZINHEIRO_EMAIL,
        telefone=DEMO_COZINHEIRO_TELEFONE,
        senha=demo_senha_placeholder_hash(),
        cep="51021-330",
        rua="Rua Ernesto de Paula Santos",
        numero=187,
        complemento=None,
        especialidade_id=esp.id,
        tipo_entrega="ambos",
    )
    db.add(c)
    db.flush()
    ensure_demo_marmita(db, c.id)
    return c


def ensure_demo_marmita(db, cozinheiro_id: int) -> Any:
    from views.models import Marmita

    m = db.query(Marmita).filter(Marmita.cozinheiro_id == cozinheiro_id).first()
    if m:
        if m.nome and 'demonstração' in (m.nome or '').lower():
            m.nome = 'Marmitas'
            db.flush()
        return m
    m = Marmita(
        nome="Marmitas",
        foto=None,
        preco=Decimal("24.90"),
        cozinheiro_id=cozinheiro_id,
    )
    db.add(m)
    db.flush()
    return m
