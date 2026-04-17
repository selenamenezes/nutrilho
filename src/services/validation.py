"""Regras de cadastro e login compartilhadas entre formulário HTML (`web-prototype/`) e rotas JSON `/api/*`."""
from __future__ import annotations

import re
from typing import Optional

from werkzeug.security import generate_password_hash

from views.models import (
    Cliente,
    Cozinheiro,
    Especialidade,
)


def registration_error_code(message: str) -> Optional[str]:
    """Classifica mensagens de erro de cadastro para o app (JSON `error_code`)."""
    m = (message or "").lower()
    if "e-mail" in m and "cadastrado" in m:
        return "EMAIL_TAKEN"
    return None


def validar_email(db, email, tipo_usuario=None, id_atual=None):
    """Valida formato do e-mail e se já existe em cliente ou cozinheiro (e-mail único no sistema)."""
    if not email:
        return False, "E-mail é obrigatório"

    email = email.strip()
    padrao_email = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(padrao_email, email):
        return False, "E-mail inválido. Use um formato como usuario@exemplo.com"

    cliente_row = db.query(Cliente).filter(Cliente.email == email).first()
    cozinheiro_row = db.query(Cozinheiro).filter(Cozinheiro.email == email).first()

    if tipo_usuario == "cozinheiro":
        if cozinheiro_row and (not id_atual or cozinheiro_row.id != id_atual):
            return False, "Este e-mail já está cadastrado."
        if cliente_row:
            return False, "Este e-mail já está cadastrado."
    else:
        if cliente_row and (not id_atual or cliente_row.id != id_atual):
            return False, "Este e-mail já está cadastrado."
        if cozinheiro_row:
            return False, "Este e-mail já está cadastrado."

    return True, email


def validar_telefone(db, telefone, tipo_usuario=None, id_atual=None):
    """Valida formato do telefone e verifica se já existe"""
    if not telefone:
        return False, "Telefone é obrigatório"

    telefone_limpo = re.sub(r'\D', '', telefone)

    if len(telefone_limpo) < 10 or len(telefone_limpo) > 11:
        return False, "Telefone inválido. Use um número com 10 ou 11 dígitos (incluindo DDD)"

    if len(telefone_limpo) == 11 and telefone_limpo[2] != '9':
        return False, "Celular com 11 dígitos deve começar com 9 após o DDD"

    if tipo_usuario == 'cozinheiro':
        existente = db.query(Cozinheiro).filter(Cozinheiro.telefone == telefone_limpo)
        if id_atual:
            existente = existente.filter(Cozinheiro.id != id_atual)
        if existente.first():
            return False, "Este telefone já está cadastrado"
    else:
        existente = db.query(Cliente).filter(Cliente.telefone == telefone_limpo)
        if id_atual:
            existente = existente.filter(Cliente.id != id_atual)
        if existente.first():
            return False, "Este telefone já está cadastrado"

    return True, telefone_limpo


def validar_senha(senha, confirmar_senha=None):
    """Valida força da senha"""
    if not senha:
        return False, "Senha é obrigatória"

    if confirmar_senha is not None and senha != confirmar_senha:
        return False, "As senhas não conferem"

    if len(senha) < 6:
        return False, "A senha deve ter no mínimo 6 caracteres"

    if len(senha) > 50:
        return False, "A senha deve ter no máximo 50 caracteres"

    if not re.search(r'[A-Z]', senha):
        return False, "A senha deve conter pelo menos uma letra maiúscula"

    if not re.search(r'[a-z]', senha):
        return False, "A senha deve conter pelo menos uma letra minúscula"

    if not re.search(r'[0-9]', senha):
        return False, "A senha deve conter pelo menos um número"

    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', senha):
        return False, "A senha deve conter pelo menos um caractere especial (!@#$%^&* etc.)"

    return True, generate_password_hash(senha)


def _cep_digits(cep_raw: str) -> str:
    return re.sub(r"\D", "", cep_raw or "")


def _rua_para_banco(logradouro: str, bairro: str, localidade: str, uf: str) -> str:
    """Uma linha para `Cliente.rua` / `Cozinheiro.rua` (limite 255 no modelo)."""
    logradouro = (logradouro or "").strip()
    bairro = (bairro or "").strip()
    localidade = (localidade or "").strip()
    uf = (uf or "").strip()
    if not logradouro:
        return ""
    if bairro and localidade and uf:
        line = f"{logradouro}, {bairro} — {localidade}/{uf}"
    elif localidade and uf:
        line = f"{logradouro} — {localidade}/{uf}"
    else:
        line = logradouro
    if len(line) > 255:
        return line[:254] + "…"
    return line


def _build_cliente_cadastro(db, data):
    """Monta um `Cliente` a partir de um dict (form ou JSON). Retorna (Cliente, None) ou (None, mensagem_erro)."""
    nome = data.get('nome', '').strip()
    email = data.get('email', '').strip()
    telefone = data.get('telefone', '').strip()
    senha = data.get('senha', '')
    confirmar_senha = data.get('confirmar_senha', '')

    cep = _cep_digits(data.get('cep', ''))
    logradouro = data.get('logradouro', '').strip()
    bairro = data.get('bairro', '').strip()
    localidade = data.get('localidade', '').strip()
    uf = data.get('uf', '').strip()
    numero = str(data.get('numero', '0')).strip()
    complemento = data.get('complemento', '').strip()
    restricao = data.get('restricao', '') or ''
    restricao = restricao.strip() if isinstance(restricao, str) else ''

    objetivos = data.get('objetivos')
    if isinstance(objetivos, str):
        objetivos = [x.strip() for x in objetivos.split(',') if x.strip()] if objetivos.strip() else []
    elif isinstance(objetivos, list):
        objetivos = [str(x).strip() for x in objetivos if str(x).strip()]
    else:
        objetivos = []

    if not nome:
        return None, "Nome é obrigatório"
    if len(cep) != 8:
        return None, "CEP é obrigatório (8 dígitos)"
    if not logradouro:
        return None, "Logradouro (rua) é obrigatório"
    if not numero or not numero.isdigit():
        return None, "Número é obrigatório e deve conter apenas dígitos"

    rua_banco = _rua_para_banco(logradouro, bairro, localidade, uf)
    if not rua_banco:
        return None, "Endereço incompleto"

    valido, email_valido = validar_email(db, email)
    if not valido:
        return None, email_valido

    valido, telefone_valido = validar_telefone(db, telefone)
    if not valido:
        return None, telefone_valido

    valido, senha_hash = validar_senha(senha, confirmar_senha)
    if not valido:
        return None, senha_hash

    blocos = []
    if objetivos:
        blocos.append("Objetivos alimentares: " + ", ".join(objetivos))
    if restricao:
        blocos.append("Restrições alimentares: " + restricao)
    restricao_final = "\n\n".join(blocos) if blocos else None

    cep_fmt = f"{cep[:5]}-{cep[5:]}"
    novo_cliente = Cliente(
        nome=nome,
        email=email_valido,
        telefone=telefone_valido,
        senha=senha_hash,
        cep=cep_fmt,
        rua=rua_banco,
        numero=int(numero) if numero.isdigit() else 0,
        complemento=complemento or None,
        restricao=restricao_final,
    )
    return novo_cliente, None


def _build_cozinheiro_cadastro(db, data):
    """Monta um `Cozinheiro` a partir de um dict (form ou JSON). Retorna (Cozinheiro, None) ou (None, mensagem_erro)."""
    nome = data.get('nome', '').strip()
    email = data.get('email', '').strip()
    telefone = data.get('telefone', '').strip()
    senha = data.get('senha', '')
    confirmar_senha = data.get('confirmar_senha', '')

    cep = _cep_digits(data.get('cep', ''))
    logradouro = data.get('logradouro', '').strip()
    bairro = data.get('bairro', '').strip()
    localidade = data.get('localidade', '').strip()
    uf = data.get('uf', '').strip()
    numero = str(data.get('numero', '0')).strip()
    complemento = data.get('complemento', '').strip()

    especialidades = data.get('especialidades')
    if isinstance(especialidades, str):
        especialidades = [especialidades] if especialidades.strip() else []
    elif isinstance(especialidades, list):
        especialidades = [str(x).strip() for x in especialidades if str(x).strip()]
    else:
        especialidades = []
    if not especialidades and data.get('especialidade_nome'):
        especialidades = [str(data['especialidade_nome']).strip()]

    sobre_voce = data.get('sobre_voce', '')
    foto_link = data.get('foto_link', '').strip()
    tipo_entrega = data.get('tipo_entrega', '').strip()

    if not nome:
        return None, "Nome é obrigatório"
    if len(cep) != 8:
        return None, "CEP é obrigatório (8 dígitos)"
    if not logradouro:
        return None, "Logradouro (rua) é obrigatório"
    if not numero or not str(numero).strip().isdigit():
        return None, "Número é obrigatório e deve conter apenas dígitos"

    rua_banco = _rua_para_banco(logradouro, bairro, localidade, uf)
    if not rua_banco:
        return None, "Endereço incompleto"

    valido, email_valido = validar_email(db, email, 'cozinheiro')
    if not valido:
        return None, email_valido

    valido, telefone_valido = validar_telefone(db, telefone, 'cozinheiro')
    if not valido:
        return None, telefone_valido

    valido, senha_hash = validar_senha(senha, confirmar_senha)
    if not valido:
        return None, senha_hash

    tipos_entrega_validos = ['delivery', 'retirada', 'ambos']
    if tipo_entrega and tipo_entrega not in tipos_entrega_validos:
        return None, "Tipo de entrega inválido"

    especialidade_nome = especialidades[0] if especialidades else 'Geral'
    especialidade = db.query(Especialidade).filter(Especialidade.nome == especialidade_nome).first()
    if not especialidade:
        especialidade = Especialidade(nome=especialidade_nome)
        db.add(especialidade)
        db.commit()
        db.refresh(especialidade)

    if len(especialidades) > 1:
        prefix = "Especialidades: " + ", ".join(especialidades) + "\n\n"
        sobre_voce = prefix + (sobre_voce or '')

    cep_fmt = f"{cep[:5]}-{cep[5:]}"
    novo_cozinheiro = Cozinheiro(
        nome=nome,
        email=email_valido,
        telefone=telefone_valido,
        senha=senha_hash,
        rua=rua_banco,
        cep=cep_fmt,
        complemento=complemento or None,
        numero=int(numero) if numero.isdigit() else 0,
        especialidade_id=especialidade.id,
        sobre_voce=sobre_voce,
        foto_link=foto_link if foto_link else None,
        tipo_entrega=tipo_entrega if tipo_entrega else None,
        avaliacao=0
    )
    return novo_cozinheiro, None
