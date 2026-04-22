from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from flask_cors import CORS
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
import sys
import os
import requests
from werkzeug.security import check_password_hash
from sqlalchemy.exc import IntegrityError
import re
import uuid
from decimal import Decimal
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from sqlalchemy import text, or_

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SRC_DIR, ".env"))

sys.path.append(_SRC_DIR)

from database import SessionLocal, engine, Base
from views.models import Cozinheiro, Cliente, Pedido, Especialidade, Proposta, Marmita, Solicitacao
from services.validation import (
    validar_email,
    validar_telefone,
    validar_senha,
    _build_cliente_cadastro,
    _build_cozinheiro_cadastro,
    registration_error_code,
)
from services.brasilapi import fetch_lat_lon_por_cep, _norm_cep
from utils.geo import distancia_km, bucket_distancia_km
app = Flask(
    __name__,
    template_folder='../../web-prototype',
    static_folder='../../web-prototype',
)

app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-change-me-set-FLASK_SECRET_KEY')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

_cors_origins = os.environ.get('CORS_ORIGINS', '').strip()
if _cors_origins:
    CORS(
        app,
        supports_credentials=True,
        origins=[o.strip() for o in _cors_origins.split(',') if o.strip()],
    )
else:
    CORS(app, supports_credentials=True)

Base.metadata.create_all(bind=engine)

UPLOAD_DIR = os.path.join(_SRC_DIR, 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOAD_EXT = {'.pdf', '.jpg', '.jpeg', '.png'}


def _migrate_solicitacoes_extra_columns():
    """Adiciona colunas em bases já existentes (MySQL). Tabelas novas já vêm do create_all."""
    stmts = [
        "ALTER TABLE solicitacoes ADD COLUMN situacao VARCHAR(50) DEFAULT 'aguardando_cozinheiro'",
        "ALTER TABLE solicitacoes ADD COLUMN demo_convite_recusado INT NOT NULL DEFAULT 0",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            low = str(e).lower()
            if 'duplicate' in low or '1060' in str(e):
                continue
            print(f'[migrate solicitacoes] {e}')


_migrate_solicitacoes_extra_columns()


def _migrate_solicitacoes_null_situacao():
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE solicitacoes SET situacao = 'aguardando_cozinheiro' "
                "WHERE situacao IS NULL OR situacao = ''"
            ))
    except Exception as e:
        print(f'[migrate solicitacoes null situacao] {e}')


_migrate_solicitacoes_null_situacao()


def _migrate_proposta_extra_columns():
    """Adiciona colunas novas à tabela `proposta` em bases existentes (MySQL)."""
    stmts = [
        "ALTER TABLE proposta ADD COLUMN data_resposta DATETIME NULL",
        "ALTER TABLE proposta ADD COLUMN tempo_entrega_min INT NULL",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            low = str(e).lower()
            if 'duplicate' in low or '1060' in str(e):
                continue
            print(f'[migrate proposta] {e}')


_migrate_proposta_extra_columns()


def _migrate_cozinheiro_entrega_columns():
    """Colunas de configuração de entrega no cozinheiro (PLAN_USUARIO §9.2)."""
    stmts = [
        "ALTER TABLE cozinheiros ADD COLUMN taxa_motoboy DECIMAL(8,2) NULL",
        "ALTER TABLE cozinheiros ADD COLUMN aceita_parceiros TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE cozinheiros ADD COLUMN taxa_parceiros DECIMAL(8,2) NULL",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            low = str(e).lower()
            if 'duplicate' in low or '1060' in str(e):
                continue
            print(f'[migrate cozinheiros] {e}')


_migrate_cozinheiro_entrega_columns()


def _migrate_pedido_entrega_columns():
    """Colunas da entrega escolhida pelo cliente (PLAN_USUARIO §9.2)."""
    stmts = [
        "ALTER TABLE pedidos ADD COLUMN entrega_opcao VARCHAR(32) NULL",
        "ALTER TABLE pedidos ADD COLUMN taxa_entrega DECIMAL(8,2) NULL",
        "ALTER TABLE pedidos ADD COLUMN tempo_entrega_min INT NULL",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            low = str(e).lower()
            if 'duplicate' in low or '1060' in str(e):
                continue
            print(f'[migrate pedidos] {e}')


_migrate_pedido_entrega_columns()


def _migrate_geo_columns():
    """Colunas de geolocalização no cliente e no cozinheiro (PLAN §10/§11).

    Populadas a partir de BrasilAPI/CEP. Idempotente — ignora `1060/duplicate`.
    """
    stmts = [
        "ALTER TABLE cozinheiros ADD COLUMN latitude DECIMAL(10,7) NULL",
        "ALTER TABLE cozinheiros ADD COLUMN longitude DECIMAL(10,7) NULL",
        "ALTER TABLE cozinheiros ADD COLUMN geo_cep_ref VARCHAR(16) NULL",
        "ALTER TABLE cozinheiros ADD COLUMN geo_atualizado_em DATETIME NULL",
        "ALTER TABLE cliente ADD COLUMN latitude DECIMAL(10,7) NULL",
        "ALTER TABLE cliente ADD COLUMN longitude DECIMAL(10,7) NULL",
        "ALTER TABLE cliente ADD COLUMN geo_cep_ref VARCHAR(16) NULL",
        "ALTER TABLE cliente ADD COLUMN geo_atualizado_em DATETIME NULL",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            low = str(e).lower()
            if 'duplicate' in low or '1060' in str(e):
                continue
            print(f'[migrate geo] {e}')


_migrate_geo_columns()


def _migrate_pedido_pagamento_columns():
    """Colunas do checkout fake no pedido (PLAN_USUARIO §12)."""
    stmts = [
        "ALTER TABLE pedidos ADD COLUMN status_pagamento VARCHAR(16) NOT NULL DEFAULT 'pendente'",
        "ALTER TABLE pedidos ADD COLUMN metodo_pagamento VARCHAR(16) NULL",
        "ALTER TABLE pedidos ADD COLUMN pix_copia_cola VARCHAR(255) NULL",
        "ALTER TABLE pedidos ADD COLUMN pagamento_data DATETIME NULL",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as e:
            low = str(e).lower()
            if 'duplicate' in low or '1060' in str(e):
                continue
            print(f'[migrate pagamento] {e}')


_migrate_pedido_pagamento_columns()


def _ensure_geo_for_user(db, user) -> bool:
    """Garante que `user.latitude/longitude` reflete o `user.cep` atual.

    Refetcha BrasilAPI quando:
      - `geo_cep_ref` é diferente do CEP atual (usuário mudou endereço);
      - `latitude/longitude` estão NULL (conta antiga sem geo).

    Retorna `True` quando escreveu algo no `user`. O commit é
    responsabilidade do chamador — se o caller já está num contexto
    transacional (ex.: PUT /api/perfil), evita flushes parciais.

    Best-effort: qualquer falha em BrasilAPI só marca `geo_cep_ref` para
    não ficar tentando a cada request (`fetch_lat_lon_por_cep` já tem
    cache próprio). Nunca levanta exceção.
    """
    if user is None:
        return False
    cep_atual = _norm_cep(getattr(user, 'cep', None))
    if not cep_atual:
        return False
    cep_ref = _norm_cep(getattr(user, 'geo_cep_ref', None))
    ja_tem_coords = (
        getattr(user, 'latitude', None) is not None
        and getattr(user, 'longitude', None) is not None
    )
    if ja_tem_coords and cep_ref == cep_atual:
        return False
    try:
        coords = fetch_lat_lon_por_cep(cep_atual)
    except Exception as e:
        print(f'[geo] BrasilAPI lookup failed: {e}')
        coords = None
    agora = datetime.now()
    if coords is not None:
        lat, lon = coords
        user.latitude = Decimal(str(lat))
        user.longitude = Decimal(str(lon))
    else:
        user.latitude = None
        user.longitude = None
    user.geo_cep_ref = cep_atual
    user.geo_atualizado_em = agora
    return True


def _safe_int(v, default=None):
    if v is None or v == '':
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _unlink_receita_upload(receita_link):
    if not receita_link or 'uploads/' not in receita_link:
        return
    name = receita_link.rstrip('/').rsplit('/', 1)[-1]
    name = secure_filename(name)
    if not name:
        return
    path = os.path.join(UPLOAD_DIR, name)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            print(f'[uploads] remove failed: {e}')


def _register_debug_log(route: str, data) -> None:
    """Log incoming registration JSON (passwords redacted). Set REGISTER_DEBUG=0 to disable."""
    if os.environ.get('REGISTER_DEBUG', '1').lower() in ('0', 'false', 'no'):
        return
    if not isinstance(data, dict):
        print(f'[REGISTER {route}] body type={type(data).__name__!r}')
        return
    safe = {}
    for k, v in data.items():
        lk = k.lower()
        if 'senha' in lk:
            safe[k] = '***'
        else:
            safe[k] = v
    print(f'[REGISTER {route}] keys={list(data.keys())} payload={safe}')


def _register_error_response(err_msg: str):
    """Resposta JSON de erro de cadastro; `409` + `error_code` quando e-mail já existe."""
    code = registration_error_code(err_msg)
    payload = {'success': False, 'error': err_msg}
    if code:
        payload['error_code'] = code
    status = 409 if code == 'EMAIL_TAKEN' else 400
    return jsonify(payload), status


def _register_integrity_response(exc: IntegrityError):
    low = str(getattr(exc, 'orig', exc)).lower()
    if 'email' in low:
        return jsonify({
            'success': False,
            'error': 'Este e-mail já está cadastrado.',
            'error_code': 'EMAIL_TAKEN',
        }), 409
    if 'telefone' in low or 'telephone' in low:
        return jsonify({
            'success': False,
            'error': 'Este telefone já está cadastrado.',
        }), 409
    return jsonify({'success': False, 'error': 'Não foi possível concluir o cadastro.'}), 400


# ============ ROTAS DAS PÁGINAS HTML (web-prototype — cadastro, login, etc.) ============

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/cadastro')
def cadastro():
    return render_template('cadastro.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/home-user')
def home_user():
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return redirect('/login')
    return render_template('home-user.html')

@app.route('/enviar-receita')
def enviar_receita():
    if 'usuario_id' not in session:
        return redirect('/login')
    return render_template('enviar-receita.html')

@app.route('/cozinheiros')
def cozinheiros_page():
    return render_template('cozinheiros.html')

@app.route('/confirmar')
def confirmar():
    if 'usuario_id' not in session:
        return redirect('/login')
    return render_template('confirmar.html')

@app.route('/status')
def status_page():
    if 'usuario_id' not in session:
        return redirect('/login')
    return render_template('status.html')

@app.route('/meus-pedidos')
def meus_pedidos():
    if 'usuario_id' not in session:
        return redirect('/login')
    return render_template('meus-pedidos.html')

@app.route('/avaliacao')
def avaliacao():
    if 'usuario_id' not in session:
        return redirect('/login')
    return render_template('avaliacao.html')

@app.route('/perfil')
def perfil():
    if 'usuario_id' not in session:
        return redirect('/login')
    return render_template('perfil.html')

@app.route('/cardapios')
def cardapios():
    return render_template('cardapios.html')

@app.route('/painel-cozinheiro')
def painel_cozinheiro():
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cozinheiro':
        return redirect('/login')
    return render_template('painel-cozinheiro.html')

# ============ API: BUSCAR CEP ============
def _cep_from_request():
    """Compatível com `cadastro.html` (form), JSON (`{"cep":"..."}`) e query `?cep=`."""
    raw = (request.form.get('cep') or '').strip()
    if not raw:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            raw = (data.get('cep') or '').strip()
    if not raw:
        raw = (request.args.get('cep') or '').strip()
    return re.sub(r'\D', '', raw)


@app.route('/api/buscar-cep', methods=['POST', 'GET'])
def buscar_cep():
    """Consulta ViaCEP. Persistência no BD: apenas cep + logradouro (rua) + número + complemento."""
    try:
        cep = _cep_from_request()

        if not cep or not cep.isdigit() or len(cep) != 8:
            return jsonify({'success': False, 'error': 'CEP inválido. Digite 8 números.'}), 400
        
        response = requests.get(f'https://viacep.com.br/ws/{cep}/json/', timeout=5)
        response.raise_for_status()
        
        endereco = response.json()
        
        if 'erro' in endereco:
            return jsonify({'success': False, 'error': 'CEP não encontrado.'}), 404
        
        return jsonify({
            'success': True,
            'logradouro': endereco.get('logradouro', ''),
            'bairro': endereco.get('bairro', ''),
            'cidade': endereco.get('localidade', ''),
            'uf': endereco.get('uf', ''),
            'cep': endereco.get('cep', '')
        })
        
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': 'A consulta demorou muito. Tente novamente.'}), 504
    except Exception as e:
        print(f"Erro ao buscar CEP: {e}")
        return jsonify({'success': False, 'error': 'Erro ao consultar o CEP.'}), 500

# ============ ROTA DE CADASTRO (via POST de formulário) ============
@app.route('/cadastro', methods=['POST'])
def processar_cadastro():
    """Processa o cadastro vindo do formulário HTML"""
    tipo = request.form.get('tipo_cadastro', 'cliente')
    
    if tipo == 'cliente':
        return cadastrar_cliente_form()
    else:
        return cadastrar_cozinheiro_form()

def cadastrar_cliente_form():
    """Cadastra um novo cliente via formulário HTML"""
    db = SessionLocal()
    try:
        data = {
            'nome': request.form.get('nome', '').strip(),
            'email': request.form.get('email', '').strip(),
            'telefone': request.form.get('telefone', '').strip(),
            'senha': request.form.get('senha', ''),
            'confirmar_senha': request.form.get('confirmar_senha', ''),
            'cep': request.form.get('cep', '').strip(),
            'logradouro': request.form.get('logradouro', '').strip(),
            'numero': request.form.get('numero', '0').strip(),
            'complemento': request.form.get('complemento', '').strip(),
            'restricao': request.form.get('restricao', ''),
        }
        novo_cliente, err = _build_cliente_cadastro(db, data)
        if err:
            return render_template('cadastro.html', erro=err)

        db.add(novo_cliente)
        db.commit()
        db.refresh(novo_cliente)
        
        session['usuario_id'] = novo_cliente.id
        session['usuario_tipo'] = 'cliente'
        session['usuario_nome'] = novo_cliente.nome
        session['usuario_email'] = novo_cliente.email
        
        return redirect(url_for('home_user'))
        
    except Exception as e:
        db.rollback()
        print(f"Erro no cadastro de cliente: {e}")
        import traceback
        traceback.print_exc()
        return render_template('cadastro.html', erro=f"Erro ao cadastrar: {str(e)}")
    finally:
        db.close()

def cadastrar_cozinheiro_form():
    """Cadastra um novo cozinheiro via formulário HTML"""
    db = SessionLocal()
    try:
        data = {
            'nome': request.form.get('nome', '').strip(),
            'email': request.form.get('email', '').strip(),
            'telefone': request.form.get('telefone', '').strip(),
            'senha': request.form.get('senha', ''),
            'confirmar_senha': request.form.get('confirmar_senha', ''),
            'cep': request.form.get('cep', '').strip(),
            'logradouro': request.form.get('logradouro', '').strip(),
            'numero': request.form.get('numero', '0').strip(),
            'complemento': request.form.get('complemento', '').strip(),
            'especialidades': request.form.getlist('especialidades'),
            'sobre_voce': request.form.get('sobre_voce', ''),
            'foto_link': request.form.get('foto_link', '').strip(),
            'tipo_entrega': request.form.get('tipo_entrega', '').strip(),
        }
        novo_cozinheiro, err = _build_cozinheiro_cadastro(db, data)
        if err:
            return render_template('cadastro.html', erro=err)

        db.add(novo_cozinheiro)
        db.commit()
        db.refresh(novo_cozinheiro)
        
        session['usuario_id'] = novo_cozinheiro.id
        session['usuario_tipo'] = 'cozinheiro'
        session['usuario_nome'] = novo_cozinheiro.nome
        session['usuario_email'] = novo_cozinheiro.email
        
        return redirect(url_for('painel_cozinheiro'))
        
    except Exception as e:
        db.rollback()
        print(f"Erro no cadastro de cozinheiro: {e}")
        import traceback
        traceback.print_exc()
        return render_template('cadastro.html', erro=f"Erro ao cadastrar: {str(e)}")
    finally:
        db.close()


# ============ API: CADASTRO (JSON — app mobile / SPA) ============
@app.route('/api/register/cliente', methods=['POST'])
def api_register_cliente():
    """Cadastro de cliente via JSON (mesmas regras do formulário HTML). Abre sessão após sucesso."""
    db = SessionLocal()
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'success': False, 'error': 'Envie um JSON válido'}), 400
        _register_debug_log('POST /api/register/cliente', data)
        novo, err = _build_cliente_cadastro(db, data)
        if err:
            print(f'[REGISTER POST /api/register/cliente] validation error: {err!r}')
            return _register_error_response(err)
        db.add(novo)
        try:
            db.commit()
        except IntegrityError as ie:
            db.rollback()
            return _register_integrity_response(ie)
        db.refresh(novo)
        try:
            if _ensure_geo_for_user(db, novo):
                db.commit()
        except Exception as e:
            db.rollback()
            print(f'[register cliente] geo falhou: {e}')
        session['usuario_id'] = novo.id
        session['usuario_tipo'] = 'cliente'
        session['usuario_nome'] = novo.nome
        session['usuario_email'] = novo.email
        print(f'[REGISTER POST /api/register/cliente] ok usuario_id={novo.id} email={novo.email!r}')
        return jsonify({
            'success': True,
            'message': 'Cadastro realizado com sucesso!',
            'usuario_id': novo.id,
            'usuario_tipo': 'cliente',
            'usuario_nome': novo.nome,
            'usuario_email': novo.email,
        })
    except Exception as e:
        db.rollback()
        print(f"Erro no cadastro API cliente: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Erro ao cadastrar: {str(e)}'}), 500
    finally:
        db.close()


@app.route('/api/register/cozinheiro', methods=['POST'])
def api_register_cozinheiro():
    """Cadastro de cozinheiro via JSON. Campos de especialidade: `especialidades` (lista) ou `especialidade_nome`."""
    db = SessionLocal()
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'success': False, 'error': 'Envie um JSON válido'}), 400
        _register_debug_log('POST /api/register/cozinheiro', data)
        novo, err = _build_cozinheiro_cadastro(db, data)
        if err:
            print(f'[REGISTER POST /api/register/cozinheiro] validation error: {err!r}')
            return _register_error_response(err)
        db.add(novo)
        try:
            db.commit()
        except IntegrityError as ie:
            db.rollback()
            return _register_integrity_response(ie)
        db.refresh(novo)
        try:
            if _ensure_geo_for_user(db, novo):
                db.commit()
        except Exception as e:
            db.rollback()
            print(f'[register cozinheiro] geo falhou: {e}')
        session['usuario_id'] = novo.id
        session['usuario_tipo'] = 'cozinheiro'
        session['usuario_nome'] = novo.nome
        session['usuario_email'] = novo.email
        print(f'[REGISTER POST /api/register/cozinheiro] ok usuario_id={novo.id} email={novo.email!r}')
        return jsonify({
            'success': True,
            'message': 'Cadastro realizado com sucesso!',
            'usuario_id': novo.id,
            'usuario_tipo': 'cozinheiro',
            'usuario_nome': novo.nome,
            'usuario_email': novo.email,
        })
    except Exception as e:
        db.rollback()
        print(f"Erro no cadastro API cozinheiro: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': f'Erro ao cadastrar: {str(e)}'}), 500
    finally:
        db.close()


# ============ API: LOGIN ============
@app.route('/api/login', methods=['POST'])
def login():
    """Faz login de cliente ou cozinheiro"""
    db = SessionLocal()
    try:
        data = request.get_json(silent=True) or {}
        email = data.get('email')
        senha = data.get('senha')
        tipo = data.get('tipo', 'cliente')
        
        if not email or not senha:
            return jsonify({'success': False, 'error': 'Preencha email e senha'}), 400
        
        if tipo == 'cliente':
            usuario = db.query(Cliente).filter(Cliente.email == email).first()
            if not usuario or not check_password_hash(usuario.senha, senha):
                return jsonify({'success': False, 'error': 'Email ou senha incorretos'}), 401
            
            session['usuario_id'] = usuario.id
            session['usuario_tipo'] = 'cliente'
            session['usuario_nome'] = usuario.nome
            session['usuario_email'] = usuario.email
            # Backfill oportunístico de geo p/ contas antigas sem lat/lon
            # (`PLAN §10/§11`). Falhas são ignoradas.
            try:
                if _ensure_geo_for_user(db, usuario):
                    db.commit()
            except Exception as e:
                db.rollback()
                print(f'[login] geo backfill cliente falhou: {e}')
            
            return jsonify({
                'success': True,
                'message': 'Login realizado com sucesso!',
                'usuario_id': usuario.id,
                'usuario_tipo': 'cliente',
                'usuario_nome': usuario.nome,
                'usuario_email': usuario.email,
                'redirect': '/home-user'
            })
            
        elif tipo == 'cozinheiro':
            usuario = db.query(Cozinheiro).filter(Cozinheiro.email == email).first()
            if not usuario or not check_password_hash(usuario.senha, senha):
                return jsonify({'success': False, 'error': 'Email ou senha incorretos'}), 401
            
            session['usuario_id'] = usuario.id
            session['usuario_tipo'] = 'cozinheiro'
            session['usuario_nome'] = usuario.nome
            session['usuario_email'] = usuario.email
            try:
                if _ensure_geo_for_user(db, usuario):
                    db.commit()
            except Exception as e:
                db.rollback()
                print(f'[login] geo backfill cozinheiro falhou: {e}')
            
            return jsonify({
                'success': True,
                'message': 'Login realizado com sucesso!',
                'usuario_id': usuario.id,
                'usuario_tipo': 'cozinheiro',
                'usuario_nome': usuario.nome,
                'usuario_email': usuario.email,
                'redirect': '/painel-cozinheiro'
            })

        return jsonify({'success': False, 'error': 'Tipo inválido. Use "cliente" ou "cozinheiro".'}), 400
            
    except Exception as e:
        print(f"Erro no login: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()

# ============ API: VERIFICAR LOGIN ============
@app.route('/api/verificar-login', methods=['GET'])
def verificar_login():
    """Verifica se o usuário está logado"""
    if 'usuario_id' in session:
        return jsonify({
            'logado': True,
            'usuario_id': session['usuario_id'],
            'usuario_tipo': session['usuario_tipo'],
            'usuario_nome': session['usuario_nome'],
            'usuario_email': session.get('usuario_email'),
        })
    return jsonify({'logado': False})

# ============ API: LOGOUT ============
@app.route('/api/logout', methods=['POST'])
def logout():
    """Faz logout do usuário"""
    session.clear()
    return jsonify({'success': True, 'redirect': '/'})

# ============ API: LISTAR COZINHEIROS ============
@app.route('/api/cozinheiros', methods=['GET'])
def listar_cozinheiros():
    """Retorna lista de cozinheiros para o marketplace.

    Inclui `distancia_km` (precisa) quando o cliente logado tem
    `latitude/longitude` populados — caso contrário, `null`. Ver PLAN §10.
    """
    db = SessionLocal()
    try:
        especialidade_filtro = request.args.get('especialidade')
        
        query = db.query(Cozinheiro)
        if especialidade_filtro:
            query = query.join(Especialidade).filter(Especialidade.nome == especialidade_filtro)
        
        cozinheiros = query.all()
        
        # Origem para cálculo de distância: cliente logado (se houver).
        origem = None
        if session.get('usuario_tipo') == 'cliente' and session.get('usuario_id'):
            cli = db.query(Cliente).filter(Cliente.id == session['usuario_id']).first()
            if cli is not None:
                # Tenta backfill oportunístico — já dá pra usar nesta resposta.
                try:
                    if _ensure_geo_for_user(db, cli):
                        db.commit()
                except Exception as e:
                    db.rollback()
                    print(f'[marketplace] geo backfill falhou: {e}')
                if cli.latitude is not None and cli.longitude is not None:
                    origem = (cli.latitude, cli.longitude)

        resultado = []
        for c in cozinheiros:
            # Calcular média de avaliações
            from sqlalchemy import func
            media_avaliacoes = db.query(func.avg(Pedido.avaliacao)).filter(
                Pedido.cozinheiro_id == c.id,
                Pedido.avaliacao > 0
            ).scalar() or 0

            d_km = None
            if origem is not None and c.latitude is not None and c.longitude is not None:
                d_km = distancia_km(origem[0], origem[1], c.latitude, c.longitude)

            resultado.append({
                'id': c.id,
                'nome': c.nome,
                'avaliacao': float(media_avaliacoes),
                'especialidade': c.especialidade.nome if c.especialidade else None,
                'localizacao': c.cep,
                'rua': c.rua,
                'sobre': c.sobre_voce,
                'foto': c.foto_link,
                'telefone': c.telefone,
                'tipo_entrega': c.tipo_entrega,
                'distancia_km': d_km,
            })
        
        return jsonify(resultado)
    finally:
        db.close()

# ============ API: DETALHES DO COZINHEIRO ============
@app.route('/api/cozinheiros/<int:cozinheiro_id>', methods=['GET'])
def detalhes_cozinheiro(cozinheiro_id):
    """Retorna detalhes de um cozinheiro específico"""
    db = SessionLocal()
    try:
        cozinheiro = db.query(Cozinheiro).filter(Cozinheiro.id == cozinheiro_id).first()
        if not cozinheiro:
            return jsonify({'error': 'Cozinheiro não encontrado'}), 404
        
        from sqlalchemy import func
        media_avaliacoes = db.query(func.avg(Pedido.avaliacao)).filter(
            Pedido.cozinheiro_id == cozinheiro_id,
            Pedido.avaliacao > 0
        ).scalar() or 0
        
        marmitas = db.query(Marmita).filter(Marmita.cozinheiro_id == cozinheiro_id).all()
        
        return jsonify({
            'id': cozinheiro.id,
            'nome': cozinheiro.nome,
            'avaliacao': float(media_avaliacoes),
            'especialidade': cozinheiro.especialidade.nome if cozinheiro.especialidade else None,
            'localizacao': cozinheiro.cep,
            'rua': cozinheiro.rua,
            'sobre': cozinheiro.sobre_voce,
            'foto': cozinheiro.foto_link,
            'telefone': cozinheiro.telefone,
            'tipo_entrega': cozinheiro.tipo_entrega,
            'marmitas': [{
                'id': m.id,
                'nome': m.nome,
                'preco': float(m.preco),
                'foto': m.foto
            } for m in marmitas]
        })
    finally:
        db.close()

# ============ API: CRIAR PEDIDO ============
@app.route('/api/pedidos', methods=['POST'])
def criar_pedido():
    """Cria um novo pedido"""
    if 'usuario_id' not in session:
        return jsonify({'success': False, 'error': 'Usuário não logado'}), 401
    
    db = SessionLocal()
    try:
        data = request.json
        
        # Validar campos obrigatórios
        if not data.get('cozinheiro_id'):
            return jsonify({'success': False, 'error': 'ID do cozinheiro é obrigatório'}), 400
        
        if not data.get('qtd_marmitas') or data['qtd_marmitas'] <= 0:
            return jsonify({'success': False, 'error': 'Quantidade de marmitas inválida'}), 400
        
        if not data.get('valor_total') or float(data['valor_total']) <= 0:
            return jsonify({'success': False, 'error': 'Valor total inválido'}), 400
        
        # Verificar se o cozinheiro existe
        cozinheiro = db.query(Cozinheiro).filter(Cozinheiro.id == data['cozinheiro_id']).first()
        if not cozinheiro:
            return jsonify({'success': False, 'error': 'Cozinheiro não encontrado'}), 404
        
        # Verificar se a marmita existe (se foi fornecida)
        marmita_id = data.get('marmita_id')
        if marmita_id:
            marmita = db.query(Marmita).filter(Marmita.id == marmita_id).first()
            if not marmita:
                return jsonify({'success': False, 'error': 'Marmita não encontrada'}), 404
            if marmita.cozinheiro_id != data['cozinheiro_id']:
                return jsonify({'success': False, 'error': 'Marmita não pertence a este cozinheiro'}), 400
        
        # Verificar se a proposta existe (se foi fornecida)
        proposta_id = data.get('proposta_id')
        if proposta_id:
            #proposta = db.query(Proposta).filter(Proposta.id == proposta_id).first()
            proposta = db.query(Proposta)\
            .options(joinedload(Proposta.solicitacao))\
                .filter(Proposta.id == proposta_id)\
                .first()
            if not proposta:
                return jsonify({'success': False, 'error': 'Proposta não encontrada'}), 404
            if proposta.cozinheiro_id != data['cozinheiro_id']:
                return jsonify({'success': False, 'error': 'Proposta não pertence a este cozinheiro'}), 400
            # Verificar se a proposta ainda está pendente
            if proposta.status_ != 0:
                return jsonify({'success': False, 'error': 'Esta proposta não está mais disponível'}), 400
        
        # Verificar se o cliente existe
        cliente = db.query(Cliente).filter(Cliente.id == session['usuario_id']).first()
        if not cliente:
            return jsonify({'success': False, 'error': 'Cliente não encontrado'}), 404
        
        # Validar se o cliente não está tentando pedir para si mesmo
        if session['usuario_id'] == data['cozinheiro_id']:
            return jsonify({'success': False, 'error': 'Você não pode fazer pedido para si mesmo'}), 400
        
        # Criar o pedido
        novo_pedido = Pedido(
            cozinheiro_id=data['cozinheiro_id'],
            cliente_id=session['usuario_id'],
            status='pendente',
            horario=datetime.now(),
            qtd_marmitas=data['qtd_marmitas'],
            val_total=Decimal(str(data['valor_total'])),
            marmita_id=marmita_id if marmita_id else None,
            proposta_id=proposta_id if proposta_id else None,
            plano_id=data.get('plano_id') if data.get('plano_id') else None,
            avaliacao=0  # Inicialmente sem avaliação
        )
        
        db.add(novo_pedido)
        db.commit()
        db.refresh(novo_pedido)
        
        # Se foi usada uma proposta, atualizar seu status para "aceita"
        if proposta_id:
            proposta.status_ = 1  # 1 = aceita
            proposta.data_aceita = datetime.now()
            db.commit()
        
        # Log do pedido criado
        print(f"Pedido #{novo_pedido.id} criado - Cliente: {cliente.nome}, Cozinheiro: {cozinheiro.nome}, Valor: R$ {novo_pedido.val_total}")
        
        return jsonify({
            'success': True, 
            'pedido_id': novo_pedido.id,
            'message': 'Pedido criado com sucesso!',
            'pedido': {
                'id': novo_pedido.id,
                'status': novo_pedido.status,
                'valor_total': float(novo_pedido.val_total),
                'qtd_marmitas': novo_pedido.qtd_marmitas,
                'horario': novo_pedido.horario.strftime('%d/%m/%Y %H:%M')
            }
        })
        
    except Exception as e:
        db.rollback()
        print(f"Erro ao criar pedido: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()
        
# ============ API: PEDIDOS DO CLIENTE (ATUALIZADO COM PROPOSTA) ============
@app.route('/api/pedidos/cliente/<int:cliente_id>', methods=['GET'])
def pedidos_do_cliente(cliente_id):
    """Retorna todos os pedidos de um cliente"""
    if 'usuario_id' not in session or session['usuario_id'] != cliente_id:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        pedidos = db.query(Pedido).filter(Pedido.cliente_id == cliente_id).order_by(Pedido.horario.desc()).all()
        
        resultado = []
        for p in pedidos:
            # Buscar informações da proposta se existir
            proposta_info = None
            if p.proposta_id:
                proposta = db.query(Proposta).filter(Proposta.id == p.proposta_id).first()
                if proposta:
                    proposta_info = {
                        'id': proposta.id,
                        'valor': float(proposta.valor),
                        'receita_link': proposta.solicitacao.receita_link if proposta.solicitacao else None
                    }
            
            entrega_opcao = getattr(p, 'entrega_opcao', None)
            taxa_entrega = getattr(p, 'taxa_entrega', None)
            resultado.append({
                'id': p.id,
                'cozinheiro_nome': p.cozinheiro.nome if p.cozinheiro else 'Desconhecido',
                'cozinheiro_id': p.cozinheiro_id,
                'status': p.status,
                'data': p.horario.strftime('%d/%m/%Y'),
                'hora': p.horario.strftime('%H:%M'),
                'qtd_marmitas': p.qtd_marmitas,
                'valor_total': float(p.val_total),
                'avaliacao': p.avaliacao,
                'marmita_nome': _marmita_nome_listagem(p.marmita),
                'proposta_id': p.proposta_id,
                'proposta': proposta_info,
                'pode_avaliar': p.status == 'entregue' and p.avaliacao == 0,
                'entrega_opcao': entrega_opcao,
                'entrega_label': _label_entrega(entrega_opcao),
                'taxa_entrega': float(taxa_entrega) if taxa_entrega is not None else None,
                'tempo_entrega_min': getattr(p, 'tempo_entrega_min', None),
                'status_pagamento': getattr(p, 'status_pagamento', None) or 'pendente',
                'metodo_pagamento': getattr(p, 'metodo_pagamento', None),
            })
        
        return jsonify(resultado)
    finally:
        db.close()


@app.route('/api/pedidos/cliente/ativos', methods=['GET'])
def pedidos_cliente_ativos():
    """Pedidos do cliente logado que ainda não foram entregues nem cancelados (para home cliente)."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'error': 'Não autorizado'}), 401

    cliente_id = session['usuario_id']
    finais = ['entregue', 'cancelado']
    db = SessionLocal()
    try:
        pedidos = (
            db.query(Pedido)
            .filter(Pedido.cliente_id == cliente_id)
            .filter(Pedido.status.notin_(finais))
            .order_by(Pedido.horario.desc())
            .all()
        )

        resultado = []
        for p in pedidos:
            proposta_info = None
            if p.proposta_id:
                proposta = db.query(Proposta).filter(Proposta.id == p.proposta_id).first()
                if proposta:
                    proposta_info = {
                        'id': proposta.id,
                        'valor': float(proposta.valor),
                        'receita_link': proposta.solicitacao.receita_link if proposta.solicitacao else None,
                    }

            resultado.append({
                'id': p.id,
                'cozinheiro_nome': p.cozinheiro.nome if p.cozinheiro else 'Desconhecido',
                'cozinheiro_id': p.cozinheiro_id,
                'status': p.status,
                'data': p.horario.strftime('%d/%m/%Y'),
                'hora': p.horario.strftime('%H:%M'),
                'qtd_marmitas': p.qtd_marmitas,
                'valor_total': float(p.val_total),
                'avaliacao': p.avaliacao,
                'marmita_nome': p.marmita.nome if p.marmita else 'Marmita Padrão',
                'proposta_id': p.proposta_id,
                'proposta': proposta_info,
                'status_pagamento': getattr(p, 'status_pagamento', None) or 'pendente',
                'metodo_pagamento': getattr(p, 'metodo_pagamento', None),
            })

        return jsonify(resultado)
    finally:
        db.close()


# ============ API: PEDIDOS DO COZINHEIRO (ATUALIZADO COM PROPOSTA) ============
@app.route('/api/pedidos/cozinheiro/<int:cozinheiro_id>', methods=['GET'])
def pedidos_do_cozinheiro(cozinheiro_id):
    """Retorna os pedidos de um cozinheiro para o painel"""
    if (
        'usuario_id' not in session
        or session.get('usuario_tipo') != 'cozinheiro'
        or session['usuario_id'] != cozinheiro_id
    ):
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        pedidos = db.query(Pedido).filter(Pedido.cozinheiro_id == cozinheiro_id).order_by(Pedido.horario.desc()).all()
        
        resultado = []
        for p in pedidos:
            # Buscar informações da proposta se existir
            proposta_info = None
            if p.proposta_id:
                proposta = db.query(Proposta).filter(Proposta.id == p.proposta_id).first()
                if proposta:
                    proposta_info = {
                        'id': proposta.id,
                        'valor': float(proposta.valor),
                        'status': proposta.status_,
                        'receita_link': proposta.solicitacao.receita_link if proposta.solicitacao else None
                    }
            
            entrega_opcao = getattr(p, 'entrega_opcao', None)
            taxa_entrega = getattr(p, 'taxa_entrega', None)
            resultado.append({
                'id': p.id,
                'cliente_nome': p.cliente.nome if p.cliente else 'Cliente',
                'cliente_id': p.cliente_id,
                'status': p.status,
                'data': p.horario.strftime('%d/%m/%Y %H:%M'),
                'qtd_marmitas': p.qtd_marmitas,
                'valor_total': float(p.val_total),
                'avaliacao': p.avaliacao,
                'proposta_id': p.proposta_id,
                'proposta': proposta_info,
                'endereco_entrega': f"{p.cliente.rua}, {p.cliente.numero} - {p.cliente.complemento if p.cliente.complemento else ''}".strip(),
                'entrega_opcao': entrega_opcao,
                'entrega_label': _label_entrega(entrega_opcao),
                'taxa_entrega': float(taxa_entrega) if taxa_entrega is not None else None,
                'tempo_entrega_min': getattr(p, 'tempo_entrega_min', None),
            })
        
        return jsonify(resultado)
    finally:
        db.close()
        
# ============ API: ATUALIZAR STATUS DO PEDIDO ============
@app.route('/api/pedidos/<int:pedido_id>/status', methods=['PUT'])
def atualizar_status_pedido(pedido_id):
    """Atualiza o status de um pedido.

    Regras:
    - Cozinheiro dono do pedido pode mover para qualquer status (como antes).
    - Cliente dono pode **apenas** confirmar a entrega: transição
      `saiu_entrega → entregue`. Qualquer outra tentativa do cliente
      retorna 403. Isso habilita o botão "Confirmar entrega" do app
      sem abrir mão do controle do cozinheiro.
    """
    db = SessionLocal()
    try:
        data = request.json or {}
        status = data.get('status')

        pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()

        if not pedido:
            return jsonify({'error': 'Pedido não encontrado'}), 404

        usuario_tipo = session.get('usuario_tipo')
        usuario_id = session.get('usuario_id')

        if usuario_tipo == 'cozinheiro' and usuario_id == pedido.cozinheiro_id:
            pass
        elif usuario_tipo == 'cliente' and usuario_id == pedido.cliente_id:
            if not (pedido.status == 'saiu_entrega' and status == 'entregue'):
                return jsonify({
                    'error': 'Cliente só pode confirmar entrega de um pedido em saiu_entrega.',
                    'status_atual': pedido.status,
                }), 403
        else:
            return jsonify({'error': 'Não autorizado'}), 401

        pedido.status = status
        db.commit()

        return jsonify({'success': True, 'status': status})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()

# ============ API: CRIAR AVALIAÇÃO ============
@app.route('/api/avaliacoes', methods=['POST'])
def criar_avaliacao():
    """Cria uma avaliação para um pedido"""
    if 'usuario_id' not in session:
        return jsonify({'error': 'Usuário não logado'}), 401
    
    db = SessionLocal()
    try:
        data = request.json
        
        # Validar campos
        pedido_id = data.get('pedido_id')
        nota = data.get('nota')
        
        if not pedido_id:
            return jsonify({'error': 'ID do pedido é obrigatório'}), 400
        
        if not nota or nota < 1 or nota > 5:
            return jsonify({'error': 'Nota inválida. Deve ser entre 1 e 5'}), 400
        
        # Buscar o pedido
        pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        
        if not pedido:
            return jsonify({'error': 'Pedido não encontrado'}), 404
        
        # Verificar se o pedido pertence ao cliente logado
        if session['usuario_id'] != pedido.cliente_id:
            return jsonify({'error': 'Não autorizado'}), 401
        
        # Verificar se o pedido já foi avaliado
        if pedido.avaliacao > 0:
            return jsonify({'error': 'Este pedido já foi avaliado'}), 400
        
        # Verificar se o pedido está entregue
        if pedido.status != 'entregue':
            return jsonify({'error': 'Apenas pedidos entregues podem ser avaliados'}), 400
        
        # Atualizar avaliação
        pedido.avaliacao = nota
        db.commit()
        
        # Atualizar média do cozinheiro
        from sqlalchemy import func
        media = db.query(func.avg(Pedido.avaliacao)).filter(
            Pedido.cozinheiro_id == pedido.cozinheiro_id,
            Pedido.avaliacao > 0
        ).scalar() or 0
        
        cozinheiro = db.query(Cozinheiro).filter(Cozinheiro.id == pedido.cozinheiro_id).first()
        if cozinheiro:
            cozinheiro.avaliacao = int(round(media))
            db.commit()
        
        # Buscar estatísticas atualizadas
        total_avaliacoes = db.query(Pedido).filter(
            Pedido.cozinheiro_id == pedido.cozinheiro_id,
            Pedido.avaliacao > 0
        ).count()
        
        return jsonify({
            'success': True, 
            'message': 'Avaliação enviada com sucesso!',
            'avaliacao': {
                'pedido_id': pedido.id,
                'nota': nota,
                'media_cozinheiro': float(media),
                'total_avaliacoes': total_avaliacoes
            }
        })
    except Exception as e:
        db.rollback()
        print(f"Erro ao criar avaliação: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()

# ============ API: DASHBOARD DO CLIENTE ============
@app.route('/api/dashboard/cliente/<int:cliente_id>', methods=['GET'])
def dashboard_cliente(cliente_id):
    """Retorna dados para o dashboard do cliente"""
    if 'usuario_id' not in session or session['usuario_id'] != cliente_id:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        from sqlalchemy import func
        
        total_pedidos = db.query(Pedido).filter(Pedido.cliente_id == cliente_id).count()
        
        pedidos_ativos = db.query(Pedido).filter(
            Pedido.cliente_id == cliente_id,
            Pedido.status.in_(['pendente', 'confirmado', 'preparando'])
        ).count()
        
        gasto_total = db.query(func.sum(Pedido.val_total)).filter(
            Pedido.cliente_id == cliente_id
        ).scalar() or 0
        
        ultimo_pedido = db.query(Pedido).filter(
            Pedido.cliente_id == cliente_id
        ).order_by(Pedido.horario.desc()).first()
        
        return jsonify({
            'total_pedidos': total_pedidos,
            'pedidos_ativos': pedidos_ativos,
            'gasto_total': float(gasto_total),
            'ultimo_pedido': {
                'status': ultimo_pedido.status if ultimo_pedido else None,
                'data': ultimo_pedido.horario.strftime('%d/%m/%Y') if ultimo_pedido else None
            } if ultimo_pedido else None
        })
    finally:
        db.close()

# ============ API: DASHBOARD DO COZINHEIRO ============
@app.route('/api/dashboard/cozinheiro/<int:cozinheiro_id>', methods=['GET'])
def dashboard_cozinheiro(cozinheiro_id):
    """Retorna dados para o dashboard do cozinheiro"""
    if 'usuario_id' not in session or session['usuario_id'] != cozinheiro_id:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        from sqlalchemy import func
        
        total_pedidos = db.query(Pedido).filter(Pedido.cozinheiro_id == cozinheiro_id).count()
        
        pedidos_pendentes = db.query(Pedido).filter(
            Pedido.cozinheiro_id == cozinheiro_id,
            Pedido.status == 'pendente'
        ).count()
        
        faturamento_total = db.query(func.sum(Pedido.val_total)).filter(
            Pedido.cozinheiro_id == cozinheiro_id,
            Pedido.status.in_(['entregue', 'confirmado'])
        ).scalar() or 0
        
        media_avaliacao = db.query(func.avg(Pedido.avaliacao)).filter(
            Pedido.cozinheiro_id == cozinheiro_id,
            Pedido.avaliacao > 0
        ).scalar() or 0
        
        return jsonify({
            'total_pedidos': total_pedidos,
            'pedidos_pendentes': pedidos_pendentes,
            'faturamento_total': float(faturamento_total),
            'media_avaliacao': float(media_avaliacao),
            'total_marmitas': db.query(Marmita).filter(Marmita.cozinheiro_id == cozinheiro_id).count()
        })
    finally:
        db.close()

# ============ API: LISTAR ESPECIALIDADES ============
@app.route('/api/especialidades', methods=['GET'])
def listar_especialidades():
    """Retorna lista de especialidades para o select"""
    db = SessionLocal()
    try:
        especialidades = db.query(Especialidade).all()
        return jsonify([{'id': e.id, 'nome': e.nome} for e in especialidades])
    finally:
        db.close()

# ============ API: PERFIL DO USUÁRIO ============
@app.route('/api/perfil', methods=['GET'])
def get_perfil():
    """Retorna os dados do perfil do usuário logado"""
    if 'usuario_id' not in session:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        if session['usuario_tipo'] == 'cliente':
            usuario = db.query(Cliente).filter(Cliente.id == session['usuario_id']).first()
            if not usuario:
                return jsonify({'error': 'Usuário não encontrado'}), 404
            
            return jsonify({
                'id': usuario.id,
                'nome': usuario.nome,
                'email': usuario.email,
                'telefone': usuario.telefone,
                'cep': usuario.cep,
                'rua': usuario.rua,
                'numero': usuario.numero,
                'complemento': usuario.complemento,
                'restricao': usuario.restricao,
                'tipo': 'cliente'
            })
        else:
            usuario = db.query(Cozinheiro).filter(Cozinheiro.id == session['usuario_id']).first()
            if not usuario:
                return jsonify({'error': 'Usuário não encontrado'}), 404
            
            taxa_motoboy = getattr(usuario, 'taxa_motoboy', None)
            taxa_parceiros = getattr(usuario, 'taxa_parceiros', None)
            return jsonify({
                'id': usuario.id,
                'nome': usuario.nome,
                'email': usuario.email,
                'telefone': usuario.telefone,
                'cep': usuario.cep,
                'rua': usuario.rua,
                'numero': usuario.numero,
                'complemento': usuario.complemento,
                'sobre_voce': usuario.sobre_voce,
                'tipo_entrega': usuario.tipo_entrega,
                'especialidade_id': usuario.especialidade_id,
                'taxa_motoboy': float(taxa_motoboy) if taxa_motoboy is not None else None,
                'aceita_parceiros': bool(getattr(usuario, 'aceita_parceiros', False)),
                'taxa_parceiros': float(taxa_parceiros) if taxa_parceiros is not None else None,
                'tipo': 'cozinheiro'
            })
    finally:
        db.close()

# ============ API: ATUALIZAR PERFIL ============
@app.route('/api/perfil', methods=['PUT'])
def atualizar_perfil():
    """Atualiza os dados do perfil do usuário logado"""
    if 'usuario_id' not in session:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        data = request.json
        
        if session['usuario_tipo'] == 'cliente':
            usuario = db.query(Cliente).filter(Cliente.id == session['usuario_id']).first()
            if not usuario:
                return jsonify({'error': 'Usuário não encontrado'}), 404
            
            # Atualizar campos
            if 'nome' in data:
                usuario.nome = data['nome']
            if 'telefone' in data:
                valido, telefone = validar_telefone(db, data['telefone'], 'cliente', session['usuario_id'])
                if not valido:
                    return jsonify({'error': telefone}), 400
                usuario.telefone = telefone
            if 'cep' in data:
                usuario.cep = data['cep']
            if 'rua' in data:
                usuario.rua = data['rua']
            if 'numero' in data:
                usuario.numero = int(data['numero']) if str(data['numero']).isdigit() else 0
            if 'complemento' in data:
                usuario.complemento = data['complemento']
            if 'restricao' in data:
                usuario.restricao = data['restricao']
            
            # Atualizar senha se fornecida
            if 'senha' in data and data['senha']:
                valido, senha_hash = validar_senha(data['senha'])
                if not valido:
                    return jsonify({'error': senha_hash}), 400
                usuario.senha = senha_hash

            # Re-geocoda se o CEP mudou (PLAN §10).
            try:
                _ensure_geo_for_user(db, usuario)
            except Exception as e:
                print(f'[perfil cliente] geo falhou: {e}')

            db.commit()
            
            # Atualizar sessão
            session['usuario_nome'] = usuario.nome
            
            return jsonify({'success': True, 'message': 'Perfil atualizado com sucesso!'})
            
        else:  # cozinheiro
            usuario = db.query(Cozinheiro).filter(Cozinheiro.id == session['usuario_id']).first()
            if not usuario:
                return jsonify({'error': 'Usuário não encontrado'}), 404
            
            # Atualizar campos
            if 'nome' in data:
                usuario.nome = data['nome']
            if 'telefone' in data:
                valido, telefone = validar_telefone(db, data['telefone'], 'cozinheiro', session['usuario_id'])
                if not valido:
                    return jsonify({'error': telefone}), 400
                usuario.telefone = telefone
            if 'cep' in data:
                usuario.cep = data['cep']
            if 'rua' in data:
                usuario.rua = data['rua']
            if 'numero' in data:
                usuario.numero = int(data['numero']) if str(data['numero']).isdigit() else 0
            if 'complemento' in data:
                usuario.complemento = data['complemento']
            if 'sobre_voce' in data:
                usuario.sobre_voce = data['sobre_voce']
            if 'tipo_entrega' in data:
                usuario.tipo_entrega = data['tipo_entrega']
            if 'especialidade_id' in data:
                especialidade = db.query(Especialidade).filter(Especialidade.id == data['especialidade_id']).first()
                if especialidade:
                    usuario.especialidade_id = especialidade.id
            # Config de entrega (PLAN_USUARIO §9.2).
            # Aceita explicitamente `null` p/ "não oferece essa forma".
            if 'taxa_motoboy' in data:
                v = data['taxa_motoboy']
                if v is None:
                    usuario.taxa_motoboy = None
                else:
                    try:
                        val = Decimal(str(v))
                    except Exception:
                        return jsonify({'error': 'taxa_motoboy inválida'}), 400
                    if val < 0:
                        return jsonify({'error': 'taxa_motoboy não pode ser negativa'}), 400
                    usuario.taxa_motoboy = val
            if 'aceita_parceiros' in data:
                usuario.aceita_parceiros = bool(data['aceita_parceiros'])
            if 'taxa_parceiros' in data:
                v = data['taxa_parceiros']
                if v is None:
                    usuario.taxa_parceiros = None
                else:
                    try:
                        val = Decimal(str(v))
                    except Exception:
                        return jsonify({'error': 'taxa_parceiros inválida'}), 400
                    if val < 0:
                        return jsonify({'error': 'taxa_parceiros não pode ser negativa'}), 400
                    usuario.taxa_parceiros = val

            # Atualizar senha se fornecida
            if 'senha' in data and data['senha']:
                valido, senha_hash = validar_senha(data['senha'])
                if not valido:
                    return jsonify({'error': senha_hash}), 400
                usuario.senha = senha_hash

            # Re-geocoda se o CEP mudou (PLAN §11).
            try:
                _ensure_geo_for_user(db, usuario)
            except Exception as e:
                print(f'[perfil cozinheiro] geo falhou: {e}')

            db.commit()
            
            # Atualizar sessão
            session['usuario_nome'] = usuario.nome
            
            return jsonify({'success': True, 'message': 'Perfil atualizado com sucesso!'})
            
    except Exception as e:
        db.rollback()
        print(f"Erro ao atualizar perfil: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============ API: CRIAR MARMITA ============
@app.route('/api/marmitas', methods=['POST'])
def criar_marmita():
    """Cria uma nova marmita para o cozinheiro"""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cozinheiro':
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        data = request.json
        
        nova_marmita = Marmita(
            nome=data['nome'],
            preco=Decimal(str(data['preco'])),
            cozinheiro_id=session['usuario_id'],
            foto=data.get('foto')
        )
        
        db.add(nova_marmita)
        db.commit()
        db.refresh(nova_marmita)
        
        return jsonify({
            'success': True,
            'marmita': {
                'id': nova_marmita.id,
                'nome': nova_marmita.nome,
                'preco': float(nova_marmita.preco),
                'foto': nova_marmita.foto
            }
        })
    except Exception as e:
        db.rollback()
        print(f"Erro ao criar marmita: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============ API: LISTAR MARMITAS DO COZINHEIRO ============
@app.route('/api/marmitas/cozinheiro/<int:cozinheiro_id>', methods=['GET'])
def listar_marmitas_cozinheiro(cozinheiro_id):
    """Retorna todas as marmitas de um cozinheiro"""
    db = SessionLocal()
    try:
        marmitas = db.query(Marmita).filter(Marmita.cozinheiro_id == cozinheiro_id).all()
        
        return jsonify([{
            'id': m.id,
            'nome': m.nome,
            'preco': float(m.preco),
            'foto': m.foto
        } for m in marmitas])
    finally:
        db.close()


# ============ API: ATUALIZAR MARMITA ============
@app.route('/api/marmitas/<int:marmita_id>', methods=['PUT'])
def atualizar_marmita(marmita_id):
    """Atualiza os dados de uma marmita"""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cozinheiro':
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        marmita = db.query(Marmita).filter(Marmita.id == marmita_id).first()
        
        if not marmita:
            return jsonify({'error': 'Marmita não encontrada'}), 404
        
        if marmita.cozinheiro_id != session['usuario_id']:
            return jsonify({'error': 'Não autorizado'}), 401
        
        data = request.json
        
        if 'nome' in data:
            marmita.nome = data['nome']
        if 'preco' in data:
            marmita.preco = Decimal(str(data['preco']))
        if 'foto' in data:
            marmita.foto = data['foto']
        
        db.commit()
        
        return jsonify({
            'success': True,
            'marmita': {
                'id': marmita.id,
                'nome': marmita.nome,
                'preco': float(marmita.preco),
                'foto': marmita.foto
            }
        })
    except Exception as e:
        db.rollback()
        print(f"Erro ao atualizar marmita: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============ API: DELETAR MARMITA ============
@app.route('/api/marmitas/<int:marmita_id>', methods=['DELETE'])
def deletar_marmita(marmita_id):
    """Deleta uma marmita"""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cozinheiro':
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        marmita = db.query(Marmita).filter(Marmita.id == marmita_id).first()
        
        if not marmita:
            return jsonify({'error': 'Marmita não encontrada'}), 404
        
        if marmita.cozinheiro_id != session['usuario_id']:
            return jsonify({'error': 'Não autorizado'}), 401
        
        # Verificar se há pedidos associados
        pedidos_associados = db.query(Pedido).filter(Pedido.marmita_id == marmita_id).count()
        if pedidos_associados > 0:
            return jsonify({'error': 'Não é possível deletar marmita com pedidos associados'}), 400
        
        db.delete(marmita)
        db.commit()
        
        return jsonify({'success': True, 'message': 'Marmita deletada com sucesso!'})
    except Exception as e:
        db.rollback()
        print(f"Erro ao deletar marmita: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============ API: CRIAR PROPOSTA ============
@app.route('/api/propostas', methods=['POST'])
def criar_proposta():
    """Cria uma nova proposta para uma solicitação.

    Regras:
      - Apenas cozinheiros autenticados.
      - `solicitacao_id` obrigatório e deve existir.
      - `Solicitacao.situacao` deve ser `aguardando_cozinheiro`
        (rejeita `convertida`, `recusada_cliente`, etc.).
      - Não permite duas propostas pendentes do mesmo cozinheiro para
        a mesma solicitação (409).
      - `valor` deve ser numérico e > 0.
    """
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cozinheiro':
        return jsonify({'error': 'Não autorizado'}), 401

    data = request.get_json(silent=True) or {}
    solicitacao_id = data.get('solicitacao_id')
    if solicitacao_id is None:
        return jsonify({'error': 'solicitacao_id é obrigatório'}), 400
    try:
        solicitacao_id = int(solicitacao_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'solicitacao_id inválido'}), 400

    try:
        valor = Decimal(str(data.get('valor')))
    except Exception:
        return jsonify({'error': 'valor inválido'}), 400
    if valor <= 0:
        return jsonify({'error': 'valor deve ser maior que zero'}), 400

    # tempo_entrega_min: obrigatório quando o cozinheiro oferece moto-boy
    # (`Cozinheiro.taxa_motoboy IS NOT NULL`), ignorado caso contrário.
    # Validação do intervalo (5..240 min) vale em ambos os casos quando informado.
    tempo_entrega_min_raw = data.get('tempo_entrega_min')
    tempo_entrega_min = None
    if tempo_entrega_min_raw is not None and tempo_entrega_min_raw != '':
        try:
            tempo_entrega_min = int(tempo_entrega_min_raw)
        except (TypeError, ValueError):
            return jsonify({'error': 'tempo_entrega_min inválido'}), 400
        if tempo_entrega_min < 5 or tempo_entrega_min > 240:
            return jsonify({
                'error': 'tempo_entrega_min deve estar entre 5 e 240 minutos.',
            }), 400

    cozinheiro_id = session['usuario_id']

    db = SessionLocal()
    try:
        sol = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
        if not sol:
            return jsonify({'error': 'Solicitação não encontrada'}), 404

        cozinheiro_logado = (
            db.query(Cozinheiro).filter(Cozinheiro.id == cozinheiro_id).first()
        )
        oferece_motoboy = (
            cozinheiro_logado is not None
            and getattr(cozinheiro_logado, 'taxa_motoboy', None) is not None
        )
        if oferece_motoboy and tempo_entrega_min is None:
            return jsonify({
                'error': 'Informe o tempo estimado de entrega (moto-boy).',
                'field': 'tempo_entrega_min',
            }), 400
        if not oferece_motoboy:
            tempo_entrega_min = None

        situacao_atual = getattr(sol, 'situacao', None) or 'aguardando_cozinheiro'
        if situacao_atual != 'aguardando_cozinheiro':
            return jsonify({
                'error': 'Esta solicitação não está mais disponível.',
                'situacao': situacao_atual,
            }), 400

        # MVP (opção A): múltiplos cozinheiros podem enviar proposta em paralelo,
        # mas cada cozinheiro só pode ter UMA proposta pendente por solicitação.
        ja_minha = (
            db.query(Proposta)
            .filter(
                Proposta.solicitacao_id == solicitacao_id,
                Proposta.cozinheiro_id == cozinheiro_id,
                Proposta.status_ == 0,
            )
            .first()
        )
        if ja_minha:
            return jsonify({
                'error': 'Você já enviou uma proposta para esta solicitação.',
                'proposta_id': ja_minha.id,
            }), 409

        nova_proposta = Proposta(
            valor=valor,
            cozinheiro_id=cozinheiro_id,
            solicitacao_id=solicitacao_id,
            status_=0,
            data_criacao=datetime.now(),
            tempo_entrega_min=tempo_entrega_min,
        )

        db.add(nova_proposta)
        db.commit()
        db.refresh(nova_proposta)

        return jsonify({
            'success': True,
            'proposta': {
                'id': nova_proposta.id,
                'valor': float(nova_proposta.valor),
                'status': nova_proposta.status_,
                'solicitacao_id': nova_proposta.solicitacao_id,
                'data_criacao': nova_proposta.data_criacao.strftime('%d/%m/%Y %H:%M'),
                'tempo_entrega_min': nova_proposta.tempo_entrega_min,
                'receita_link': nova_proposta.solicitacao.receita_link if nova_proposta.solicitacao else None,
            },
        })
    except Exception as e:
        db.rollback()
        print(f"Erro ao criar proposta: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============ API: LISTAR PROPOSTAS DO COZINHEIRO ============
@app.route('/api/cozinheiro/propostas', methods=['GET'])
def listar_propostas_do_cozinheiro_logado():
    """Inbox do cozinheiro: suas próprias propostas filtradas por status.

    Query params:
    - `status` ∈ {pendente, aceita, recusada, todas} ou {0,1,2}; default todas.
    - `desde` (ISO date/datetime); filtra `data_criacao >= desde`.
    - `limit` (int, default 50, máx 100); `offset` (int, default 0).

    Auth: sessão cozinheiro obrigatória (401 caso contrário).
    PII: `cliente_nome` reduzido via `_primeiro_nome`.
    """
    if session.get('usuario_tipo') != 'cozinheiro' or 'usuario_id' not in session:
        return jsonify({'error': 'Não autorizado'}), 401

    cozinheiro_id = session['usuario_id']
    status_raw = (request.args.get('status') or 'todas').strip().lower()
    status_map = {
        'pendente': 0, '0': 0,
        'aceita': 1, '1': 1,
        'recusada': 2, '2': 2,
        'todas': None, '': None,
    }
    if status_raw not in status_map:
        return jsonify({'error': "Parâmetro 'status' inválido (use pendente/aceita/recusada/todas)."}), 400
    status_filtro = status_map[status_raw]

    limit = _safe_int(request.args.get('limit'), 50) or 50
    offset = _safe_int(request.args.get('offset'), 0) or 0
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    desde_raw = (request.args.get('desde') or '').strip()
    desde_dt = None
    if desde_raw:
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                desde_dt = datetime.strptime(desde_raw, fmt)
                break
            except ValueError:
                continue
        if desde_dt is None:
            return jsonify({'error': "Parâmetro 'desde' inválido (use ISO date ou datetime)."}), 400

    db = SessionLocal()
    try:
        q = db.query(Proposta).filter(Proposta.cozinheiro_id == cozinheiro_id)
        if status_filtro is not None:
            q = q.filter(Proposta.status_ == status_filtro)
        if desde_dt is not None:
            q = q.filter(Proposta.data_criacao >= desde_dt)
        total = q.count()
        props = q.order_by(Proposta.data_criacao.desc()).limit(limit).offset(offset).all()

        def _status_texto(s):
            return 'Pendente' if s == 0 else 'Aceita' if s == 1 else 'Recusada'

        def _data_resposta(p):
            if p.status_ == 0:
                return None
            ref = getattr(p, 'data_resposta', None) or (p.data_aceita if p.status_ == 1 else None)
            return ref.strftime('%d/%m/%Y %H:%M') if ref else None

        out = []
        for p in props:
            sol = p.solicitacao
            cliente_nome = _primeiro_nome(sol.cliente.nome if sol and sol.cliente else '')
            out.append({
                'id': p.id,
                'solicitacao_id': p.solicitacao_id,
                'cliente_nome': cliente_nome,
                'valor': float(p.valor),
                'status': p.status_,
                'status_texto': _status_texto(p.status_),
                'data_criacao': p.data_criacao.strftime('%d/%m/%Y %H:%M') if p.data_criacao else None,
                'data_criacao_iso': p.data_criacao.isoformat() if p.data_criacao else None,
                'data_resposta_cliente': _data_resposta(p),
                'tempo_entrega_min': getattr(p, 'tempo_entrega_min', None),
            })

        return jsonify({'propostas': out, 'total': total, 'limit': limit, 'offset': offset})
    finally:
        db.close()


@app.route('/api/propostas/cozinheiro/<int:cozinheiro_id>', methods=['GET'])
def listar_propostas_cozinheiro(cozinheiro_id):
    """Retorna todas as propostas de um cozinheiro"""
    if 'usuario_id' not in session or session['usuario_id'] != cozinheiro_id:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        propostas = db.query(Proposta).filter(Proposta.cozinheiro_id == cozinheiro_id).order_by(Proposta.data_criacao.desc()).all()
        
        return jsonify([{
            'id': p.id,
            'valor': float(p.valor),
            'status': p.status_,
            'status_texto': 'Pendente' if p.status_ == 0 else 'Aceita' if p.status_ == 1 else 'Recusada',
            'data_criacao': p.data_criacao.strftime('%d/%m/%Y %H:%M'),
            'data_aceita': p.data_aceita.strftime('%d/%m/%Y %H:%M') if p.data_aceita else None,
            'receita_link': p.receita_link
        } for p in propostas])
    finally:
        db.close()


# ============ API: ATUALIZAR STATUS DA PROPOSTA ============
@app.route('/api/propostas/<int:proposta_id>/status', methods=['PUT'])
def atualizar_status_proposta(proposta_id):
    """Atualiza o status de uma proposta (admin/cliente)"""
    db = SessionLocal()
    try:
        data = request.json
        status = data.get('status')  # 0: pendente, 1: aceita, 2: recusada
        
        proposta = db.query(Proposta).filter(Proposta.id == proposta_id).first()
        
        if not proposta:
            return jsonify({'error': 'Proposta não encontrada'}), 404
        
        proposta.status_ = status
        if status == 1:  # Aceita
            proposta.data_aceita = datetime.now()
        
        db.commit()
        
        return jsonify({'success': True, 'status': status})
    except Exception as e:
        db.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============ API: ESTATÍSTICAS GERAIS ============
@app.route('/api/estatisticas/gerais', methods=['GET'])
def estatisticas_gerais():
    """Retorna estatísticas gerais do sistema"""
    db = SessionLocal()
    try:
        from sqlalchemy import func
        
        total_cozinheiros = db.query(Cozinheiro).count()
        total_clientes = db.query(Cliente).count()
        total_pedidos = db.query(Pedido).count()
        total_pedidos_entregues = db.query(Pedido).filter(Pedido.status == 'entregue').count()
        
        faturamento_total = db.query(func.sum(Pedido.val_total)).filter(
            Pedido.status == 'entregue'
        ).scalar() or 0
        
        avaliacao_media_geral = db.query(func.avg(Pedido.avaliacao)).filter(
            Pedido.avaliacao > 0
        ).scalar() or 0
        
        # Pedidos por mês (últimos 6 meses)
        pedidos_por_mes = []
        from dateutil.relativedelta import relativedelta
        
        for i in range(5, -1, -1):
            data_inicio = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) - relativedelta(months=i)
            data_fim = (data_inicio + relativedelta(months=1)) - timedelta(days=1)
            
            total = db.query(Pedido).filter(
                Pedido.horario >= data_inicio,
                Pedido.horario <= data_fim
            ).count()
            
            pedidos_por_mes.append({
                'mes': data_inicio.strftime('%B/%Y'),
                'total': total
            })
        
        return jsonify({
            'total_cozinheiros': total_cozinheiros,
            'total_clientes': total_clientes,
            'total_pedidos': total_pedidos,
            'total_pedidos_entregues': total_pedidos_entregues,
            'faturamento_total': float(faturamento_total),
            'avaliacao_media_geral': float(avaliacao_media_geral),
            'pedidos_por_mes': pedidos_por_mes,
            'taxa_sucesso': (total_pedidos_entregues / total_pedidos * 100) if total_pedidos > 0 else 0
        })
    finally:
        db.close()


# ============ API: BUSCAR PEDIDOS POR FILTROS ============
@app.route('/api/pedidos/buscar', methods=['GET'])
def buscar_pedidos():
    """Busca pedidos com filtros (admin)"""
    if 'usuario_id' not in session:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        status = request.args.get('status')
        data_inicio = request.args.get('data_inicio')
        data_fim = request.args.get('data_fim')
        cozinheiro_id = request.args.get('cozinheiro_id')
        cliente_id = request.args.get('cliente_id')
        
        query = db.query(Pedido)
        
        if status:
            query = query.filter(Pedido.status == status)
        if data_inicio:
            query = query.filter(Pedido.horario >= datetime.strptime(data_inicio, '%Y-%m-%d'))
        if data_fim:
            query = query.filter(Pedido.horario <= datetime.strptime(data_fim, '%Y-%m-%d') + timedelta(days=1))
        if cozinheiro_id:
            query = query.filter(Pedido.cozinheiro_id == int(cozinheiro_id))
        if cliente_id:
            query = query.filter(Pedido.cliente_id == int(cliente_id))
        
        pedidos = query.order_by(Pedido.horario.desc()).limit(100).all()
        
        return jsonify([{
            'id': p.id,
            'cozinheiro_nome': p.cozinheiro.nome if p.cozinheiro else 'N/A',
            'cliente_nome': p.cliente.nome if p.cliente else 'N/A',
            'status': p.status,
            'data': p.horario.strftime('%d/%m/%Y %H:%M'),
            'qtd_marmitas': p.qtd_marmitas,
            'valor_total': float(p.val_total),
            'avaliacao': p.avaliacao
        } for p in pedidos])
    finally:
        db.close()

# ============ API: BUSCAR PEDIDO POR ID (DETALHES COMPLETOS) ============
@app.route('/api/pedidos/<int:pedido_id>', methods=['GET'])
def detalhes_pedido(pedido_id):
    """Retorna detalhes completos de um pedido específico"""
    if 'usuario_id' not in session:
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        
        if not pedido:
            return jsonify({'error': 'Pedido não encontrado'}), 404
        
        # Verificar permissão (cliente, cozinheiro ou admin do pedido)
        usuario_tipo = session.get('usuario_tipo')
        if session['usuario_id'] != pedido.cliente_id and session['usuario_id'] != pedido.cozinheiro_id and usuario_tipo != 'admin':
            return jsonify({'error': 'Não autorizado'}), 401
        
        # Preparar dados da proposta se existir
        proposta_info = None
        if pedido.proposta_id:
            proposta = db.query(Proposta).filter(Proposta.id == pedido.proposta_id).first()
            if proposta:
                proposta_info = {
                    'id': proposta.id,
                    'valor': float(proposta.valor),
                    'status': proposta.status_,
                    'status_texto': 'Pendente' if proposta.status_ == 0 else 'Aceita' if proposta.status_ == 1 else 'Recusada',
                    'data_criacao': proposta.data_criacao.strftime('%d/%m/%Y %H:%M'),
                    'data_aceita': proposta.data_aceita.strftime('%d/%m/%Y %H:%M') if proposta.data_aceita else None,
                    'receita_link': proposta.solicitacao.receita_link if proposta.solicitacao else None
                }
        
        # Preparar dados da marmita se existir
        marmita_info = None
        if pedido.marmita_id:
            marmita = db.query(Marmita).filter(Marmita.id == pedido.marmita_id).first()
            if marmita:
                marmita_info = {
                    'id': marmita.id,
                    'nome': marmita.nome,
                    'preco': float(marmita.preco),
                    'foto': marmita.foto
                }
        
        # Preparar dados do plano se existir
        plano_info = None
        if pedido.plano_id:
            plano = db.query(Especialidade).filter(Especialidade.id == pedido.plano_id).first()
            if plano:
                plano_info = {
                    'id': plano.id,
                    'nome': plano.nome
                }
        
        # Verificar se o pedido pode ser avaliado
        pode_avaliar = (
            pedido.status == 'entregue' and 
            pedido.avaliacao == 0 and 
            session['usuario_id'] == pedido.cliente_id
        )
        
        # Verificar se o pedido pode ser cancelado
        pode_cancelar = (
            pedido.status in ['pendente', 'confirmado'] and
            session['usuario_id'] == pedido.cliente_id
        )
        
        # Verificar se o pedido pode ser atualizado pelo cozinheiro
        pode_atualizar_status = (
            session['usuario_id'] == pedido.cozinheiro_id and
            pedido.status not in ['entregue', 'cancelado']
        )
        
        # Lista de status possíveis para atualização
        status_disponiveis = []
        if pode_atualizar_status:
            status_flow = {
                'pendente': ['confirmado', 'cancelado'],
                'confirmado': ['preparando', 'cancelado'],
                'preparando': ['saiu_entrega', 'cancelado'],
                'saiu_entrega': ['entregue'],
                'entregue': [],
                'cancelado': []
            }
            status_disponiveis = status_flow.get(pedido.status, [])
        
        # Construir endereço completo do cliente
        endereco_cliente = f"{pedido.cliente.rua}, {pedido.cliente.numero}"
        if pedido.cliente.complemento:
            endereco_cliente += f" - {pedido.cliente.complemento}"
        
        return jsonify({
            'success': True,
            'pedido': {
                'id': pedido.id,
                'status': pedido.status,
                'status_texto': {
                    'pendente': 'Pendente',
                    'confirmado': 'Confirmado',
                    'preparando': 'Preparando',
                    'saiu_entrega': 'Saiu para Entrega',
                    'entregue': 'Entregue',
                    'cancelado': 'Cancelado'
                }.get(pedido.status, pedido.status),
                'horario': pedido.horario.strftime('%d/%m/%Y %H:%M'),
                'horario_iso': pedido.horario.isoformat(),
                'qtd_marmitas': pedido.qtd_marmitas,
                'valor_total': float(pedido.val_total),
                'valor_unitario': float(pedido.val_total / pedido.qtd_marmitas) if pedido.qtd_marmitas > 0 else 0,
                'avaliacao': pedido.avaliacao,
                'pode_avaliar': pode_avaliar,
                'pode_cancelar': pode_cancelar,
                'pode_atualizar_status': pode_atualizar_status,
                'status_disponiveis': status_disponiveis
            },
            'cliente': {
                'id': pedido.cliente.id,
                'nome': pedido.cliente.nome,
                'email': pedido.cliente.email,
                'telefone': pedido.cliente.telefone,
                'endereco_completo': endereco_cliente,
                'rua': pedido.cliente.rua,
                'numero': pedido.cliente.numero,
                'complemento': pedido.cliente.complemento if pedido.cliente.complemento else '',
                'cep': pedido.cliente.cep,
                'restricao': pedido.cliente.restricao
            } if pedido.cliente else None,
            'cozinheiro': {
                'id': pedido.cozinheiro.id,
                'nome': pedido.cozinheiro.nome,
                'email': pedido.cozinheiro.email,
                'telefone': pedido.cozinheiro.telefone,
                'especialidade': pedido.cozinheiro.especialidade.nome if pedido.cozinheiro.especialidade else None,
                'avaliacao': pedido.cozinheiro.avaliacao,
                'foto': pedido.cozinheiro.foto_link,
                'tipo_entrega': pedido.cozinheiro.tipo_entrega,
                'sobre': pedido.cozinheiro.sobre_voce
            } if pedido.cozinheiro else None,
            'proposta': proposta_info,
            'marmita': marmita_info,
            'plano': plano_info
        })
        
    except Exception as e:
        print(f"Erro ao buscar detalhes do pedido {pedido_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Erro ao buscar detalhes do pedido'}), 500
    finally:
        db.close()

# ============ API: UPLOADS (receita) ============
@app.route('/api/uploads/<path:filename>', methods=['GET'])
def serve_receita_upload(filename):
    """Arquivos enviados em POST /api/solicitacoes (PDF/JPG/PNG)."""
    safe = secure_filename(filename)
    if not safe or safe != filename.replace('\\', '/').split('/')[-1]:
        return jsonify({'error': 'Nome inválido'}), 400
    return send_from_directory(UPLOAD_DIR, safe)


def _marmita_nome_listagem(marmita):
    """Nome amigável na lista (legado: rótulos antigos → 'Marmitas')."""
    if not marmita:
        return 'Marmita Padrão'
    raw = (marmita.nome or '').strip()
    if 'demonstração' in raw.lower():
        return 'Marmitas'
    return raw


def _serialize_pedido_ativo_cliente(p, db):
    proposta_info = None
    if p.proposta_id:
        proposta = db.query(Proposta).filter(Proposta.id == p.proposta_id).first()
        if proposta:
            sol = proposta.solicitacao
            proposta_info = {
                'id': proposta.id,
                'valor': float(proposta.valor),
                'receita_link': sol.receita_link if sol else None,
            }
    entrega_opcao = getattr(p, 'entrega_opcao', None)
    taxa_entrega = getattr(p, 'taxa_entrega', None)
    tempo_entrega_min = getattr(p, 'tempo_entrega_min', None)
    return {
        'tipo': 'pedido',
        'id': p.id,
        'cozinheiro_nome': p.cozinheiro.nome if p.cozinheiro else 'Desconhecido',
        'cozinheiro_id': p.cozinheiro_id,
        'status': p.status,
        'data': p.horario.strftime('%d/%m/%Y'),
        'hora': p.horario.strftime('%H:%M'),
        'criado_em_iso': p.horario.isoformat(),
        'qtd_marmitas': p.qtd_marmitas,
        'valor_total': float(p.val_total),
        'avaliacao': p.avaliacao,
        'marmita_nome': _marmita_nome_listagem(p.marmita),
        'proposta_id': p.proposta_id,
        'proposta': proposta_info,
        'entrega_opcao': entrega_opcao,
        'entrega_label': _label_entrega(entrega_opcao),
        'taxa_entrega': float(taxa_entrega) if taxa_entrega is not None else None,
        'tempo_entrega_min': tempo_entrega_min,
        'status_pagamento': getattr(p, 'status_pagamento', None) or 'pendente',
        'metodo_pagamento': getattr(p, 'metodo_pagamento', None),
    }


ENTREGA_OPCAO_IDS = ('retirada', 'motoboy', 'uber', 'parceiros')
ENTREGA_LABELS = {
    'retirada': 'Retirada no local',
    'motoboy': 'Delivery Moto boy',
    'uber': 'Uber',
    'parceiros': 'Parceiros (iFood/Rappi)',
}
# Estimativa Uber no MVP — placeholder até termos tabela real por CEP / Distance Matrix.
UBER_ESTIMATIVA_MVP_BRL = 12.0


def _label_entrega(op_id):
    if not op_id:
        return None
    return ENTREGA_LABELS.get(op_id, op_id)


def _construir_opciones_entrega(c, prop):
    """Monta a lista de formas de entrega para `proposta_pendente`.

    Retirada sempre é oferecida e sem frete. As demais dependem da
    configuração do cozinheiro (`taxa_motoboy`, `aceita_parceiros`,
    `tipo_entrega`). Ver `PLAN_USUARIO.md §9.2` para o contrato.
    """
    out = [{
        'id': 'retirada',
        'label': 'Retirada no local',
        'taxa': 0.0,
    }]
    if c is None:
        return out
    tipo = (getattr(c, 'tipo_entrega', '') or '').strip().lower()
    oferece_delivery = tipo in ('delivery', 'ambos', 'entrega', 'motoboy')
    if not oferece_delivery:
        return out

    taxa_motoboy = getattr(c, 'taxa_motoboy', None)
    if taxa_motoboy is not None:
        out.append({
            'id': 'motoboy',
            'label': 'Delivery Moto boy',
            'taxa': float(taxa_motoboy),
        })
    out.append({
        'id': 'uber',
        'label': 'Uber',
        'taxa': UBER_ESTIMATIVA_MVP_BRL,
        'estimativa': True,
    })
    if bool(getattr(c, 'aceita_parceiros', False)):
        out.append({
            'id': 'parceiros',
            'label': 'Parceiros (iFood/Rappi)',
            'taxa': float(getattr(c, 'taxa_parceiros', 0) or 0),
        })
    return out


def _serialize_solicitacao_cliente(s, db):
    situacao = getattr(s, 'situacao', None) or 'aguardando_cozinheiro'
    demo_rec = bool(getattr(s, 'demo_convite_recusado', 0))
    proposta_pendente = None
    pend = (
        db.query(Proposta)
        .filter(Proposta.solicitacao_id == s.id, Proposta.status_ == 0)
        .first()
    )
    if pend:
        c = db.query(Cozinheiro).filter(Cozinheiro.id == pend.cozinheiro_id).first()
        opciones = _construir_opciones_entrega(c, pend)
        # Distância precisa cozinheiro → cliente para UX do modal (PLAN §10).
        d_km = None
        cliente_obj = (
            db.query(Cliente).filter(Cliente.id == s.cliente_id).first() if s else None
        )
        if c and cliente_obj:
            d_km = distancia_km(
                cliente_obj.latitude,
                cliente_obj.longitude,
                c.latitude,
                c.longitude,
            )
        proposta_pendente = {
            'id': pend.id,
            'valor': float(pend.valor),
            'base_valor': float(pend.valor),
            'cozinheiro_id': pend.cozinheiro_id,
            'cozinheiro_nome': c.nome if c else 'Cozinheiro',
            'tipo_entrega': (c.tipo_entrega or 'Combinar retirada ou entrega') if c else '',
            'opciones_entrega': opciones,
            'tempo_entrega_min': getattr(pend, 'tempo_entrega_min', None),
            'distancia_km': d_km,
        }
    rel = s.receita_link
    if rel and not rel.startswith('/'):
        rel = f'/api/uploads/{rel}'
    return {
        'tipo': 'solicitacao',
        'id': s.id,
        'situacao': situacao,
        'data': s.data_criacao.strftime('%d/%m/%Y'),
        'hora': s.data_criacao.strftime('%H:%M'),
        'criado_em_iso': s.data_criacao.isoformat(),
        'receita_link': rel,
        'demo_convite_recusado': demo_rec,
        'proposta_pendente': proposta_pendente,
    }


@app.route('/api/cliente/home-pedidos', methods=['GET'])
def cliente_home_pedidos():
    """Solicitações abertas + pedidos ativos (home cliente)."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'error': 'Não autorizado'}), 401

    cliente_id = session['usuario_id']
    finais = ['entregue', 'cancelado']
    db = SessionLocal()
    try:
        sols = (
            db.query(Solicitacao)
            .filter(Solicitacao.cliente_id == cliente_id)
            .filter(or_(Solicitacao.situacao.is_(None), Solicitacao.situacao != 'convertida'))
            .order_by(Solicitacao.data_criacao.desc())
            .all()
        )
        solicitacoes = [_serialize_solicitacao_cliente(s, db) for s in sols]

        pedidos = (
            db.query(Pedido)
            .filter(Pedido.cliente_id == cliente_id)
            .filter(Pedido.status.notin_(finais))
            .order_by(Pedido.horario.desc())
            .all()
        )
        pedidos_out = [_serialize_pedido_ativo_cliente(p, db) for p in pedidos]

        return jsonify({'solicitacoes': solicitacoes, 'pedidos': pedidos_out})
    finally:
        db.close()


@app.route('/api/solicitacoes', methods=['POST'])
def criar_solicitacao():
    """Cria solicitação (JSON ou multipart com arquivo opcional). Cliente logado."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    def _fields_from_form():
        return {
            'modo': request.form.get('modo') or 'foto',
            'refeicoes_por_dia': _safe_int(request.form.get('refeicoes_por_dia')),
            'calorias_diarias': _safe_int(request.form.get('calorias_diarias')),
            'restricoes': (request.form.get('restricoes') or '').strip() or None,
            'alimentos_proibidos': (request.form.get('alimentos_proibidos') or '').strip() or None,
            'observacoes_nutricionista': (request.form.get('observacoes_nutricionista') or '').strip() or None,
            'qtd_dias': _safe_int(request.form.get('qtd_dias')),
            'porcoes_por_refeicao': _safe_int(request.form.get('porcoes_por_refeicao')),
            'observacoes_adicionais': (request.form.get('observacoes_adicionais') or '').strip() or None,
        }

    if request.content_type and 'multipart/form-data' in request.content_type:
        data = _fields_from_form()
        f = request.files.get('file')
    else:
        body = request.get_json(silent=True) or {}
        data = {
            'modo': body.get('modo') or 'foto',
            'refeicoes_por_dia': _safe_int(body.get('refeicoes_por_dia')),
            'calorias_diarias': _safe_int(body.get('calorias_diarias')),
            'restricoes': body.get('restricoes'),
            'alimentos_proibidos': body.get('alimentos_proibidos'),
            'observacoes_nutricionista': body.get('observacoes_nutricionista'),
            'qtd_dias': _safe_int(body.get('qtd_dias')),
            'porcoes_por_refeicao': _safe_int(body.get('porcoes_por_refeicao')),
            'observacoes_adicionais': body.get('observacoes_adicionais'),
        }
        f = None

    receita_link = None
    if f and f.filename:
        raw_name = secure_filename(f.filename)
        ext = os.path.splitext(raw_name)[1].lower()
        if ext not in ALLOWED_UPLOAD_EXT:
            return jsonify({'success': False, 'error': 'Formato não permitido. Use PDF, JPG ou PNG.'}), 400
        blob = f.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            return jsonify({'success': False, 'error': 'Arquivo acima de 10MB.'}), 400
        fname = f'{uuid.uuid4().hex}{ext}'
        path = os.path.join(UPLOAD_DIR, fname)
        with open(path, 'wb') as out:
            out.write(blob)
        receita_link = f'/api/uploads/{fname}'

    db = SessionLocal()
    try:
        sol = Solicitacao(
            cliente_id=session['usuario_id'],
            receita_link=receita_link,
            refeicoes_por_dia=data.get('refeicoes_por_dia'),
            calorias_diarias=data.get('calorias_diarias'),
            restricoes=data.get('restricoes'),
            alimentos_proibidos=data.get('alimentos_proibidos'),
            observacoes_nutricionista=data.get('observacoes_nutricionista'),
            qtd_dias=data.get('qtd_dias'),
            porcoes_por_refeicao=data.get('porcoes_por_refeicao'),
            observacoes_adicionais=data.get('observacoes_adicionais'),
            situacao='aguardando_cozinheiro',
            demo_convite_recusado=0,
        )
        db.add(sol)
        db.commit()
        db.refresh(sol)
        return jsonify({
            'success': True,
            'solicitacao_id': sol.id,
            'solicitacao': _serialize_solicitacao_cliente(sol, db),
        })
    except Exception as e:
        db.rollback()
        print(f'Erro criar solicitacao: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/propostas/<int:proposta_id>/responder-cliente', methods=['POST'])
def proposta_responder_cliente(proposta_id):
    """Cliente aceita ou recusa proposta. Aceitar cria pedido."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    data = request.get_json(silent=True) or {}
    aceitar = bool(data.get('aceitar'))
    entrega_opcao_raw = data.get('entregaOpcao') or data.get('entrega_opcao')

    db = SessionLocal()
    try:
        prop = db.query(Proposta).filter(Proposta.id == proposta_id).first()
        if not prop:
            return jsonify({'success': False, 'error': 'Proposta não encontrada'}), 404
        sol = prop.solicitacao
        if not sol or sol.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Não autorizado'}), 403
        if prop.status_ != 0:
            return jsonify({'success': False, 'error': 'Proposta já respondida'}), 400

        if aceitar:
            coz = db.query(Cozinheiro).filter(Cozinheiro.id == prop.cozinheiro_id).first()
            opciones = _construir_opciones_entrega(coz, prop)
            opciones_ids = {op['id'] for op in opciones}

            # Quando o cozinheiro ofereceu mais de uma opção, o cliente é
            # obrigado a escolher explicitamente; caso contrário assume
            # retirada.
            entrega_opcao = (entrega_opcao_raw or '').strip().lower() or None
            if entrega_opcao is None:
                if len(opciones) > 1:
                    return jsonify({
                        'success': False,
                        'error': 'Escolha a forma de entrega antes de aceitar.',
                        'field': 'entregaOpcao',
                    }), 400
                entrega_opcao = 'retirada'

            if entrega_opcao not in ENTREGA_OPCAO_IDS:
                return jsonify({
                    'success': False,
                    'error': 'Forma de entrega inválida.',
                    'field': 'entregaOpcao',
                }), 400
            if entrega_opcao not in opciones_ids:
                return jsonify({
                    'success': False,
                    'error': 'Esta forma de entrega não está disponível para esta proposta.',
                    'field': 'entregaOpcao',
                }), 400

            op_escolhida = next(op for op in opciones if op['id'] == entrega_opcao)
            taxa_entrega = Decimal(str(op_escolhida.get('taxa', 0)))
            val_total = prop.valor + taxa_entrega
            tempo_entrega_min_out = (
                prop.tempo_entrega_min if entrega_opcao == 'motoboy' else None
            )

            marmita = (
                db.query(Marmita)
                .filter(Marmita.cozinheiro_id == prop.cozinheiro_id)
                .first()
            )
            rd = sol.qtd_dias or 5
            rr = sol.refeicoes_por_dia or 3
            qtd = max(1, rd * rr)
            plano_id = coz.especialidade_id if coz else None
            pedido = Pedido(
                cozinheiro_id=prop.cozinheiro_id,
                cliente_id=sol.cliente_id,
                status='confirmado',
                horario=datetime.now(),
                qtd_marmitas=qtd,
                val_total=val_total,
                marmita_id=marmita.id if marmita else None,
                proposta_id=prop.id,
                plano_id=plano_id,
                avaliacao=0,
                entrega_opcao=entrega_opcao,
                taxa_entrega=taxa_entrega,
                tempo_entrega_min=tempo_entrega_min_out,
            )
            db.add(pedido)
            db.flush()  # precisamos do pedido.id para devolver ao checkout
            prop.status_ = 1
            now = datetime.now()
            prop.data_aceita = now
            prop.data_resposta = now
            sol.situacao = 'convertida'
            pedido_id_out = pedido.id
        else:
            prop.status_ = 2
            prop.data_resposta = datetime.now()
            sol.situacao = 'recusada_cliente'
            sol.demo_convite_recusado = 1
            pedido_id_out = None

        db.commit()
        return jsonify({'success': True, 'aceitar': aceitar, 'pedido_id': pedido_id_out})
    except Exception as e:
        db.rollback()
        print(f'Erro responder proposta: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


# ============ API: SOLICITAÇÕES ABERTAS (cozinheiro) ============
def _primeiro_nome(nome_completo: str) -> str:
    """Primeiro nome + inicial do último sobrenome (ex.: 'Maria S.').
    Evita expor o nome completo do cliente na fase de descoberta."""
    if not nome_completo:
        return 'Cliente'
    partes = [p for p in nome_completo.strip().split() if p]
    if not partes:
        return 'Cliente'
    if len(partes) == 1:
        return partes[0]
    return f'{partes[0]} {partes[-1][0].upper()}.'


def _serialize_solicitacao_aberta(s, db, cozinheiro_id):
    """View da solicitação para o painel do cozinheiro (sem PII sensível).

    `cliente_distancia_bucket` (PLAN §11) é um bucket categórico; nunca
    exponha `distancia_km` precisa aqui para não vazar o endereço do
    cliente antes do aceite da proposta.
    """
    total_propostas = (
        db.query(Proposta).filter(Proposta.solicitacao_id == s.id).count()
    )
    minha = (
        db.query(Proposta)
        .filter(
            Proposta.solicitacao_id == s.id,
            Proposta.cozinheiro_id == cozinheiro_id,
        )
        .order_by(Proposta.data_criacao.desc())
        .first()
    )
    rel = s.receita_link
    if rel and not rel.startswith('/'):
        rel = f'/api/uploads/{rel}'

    minha_out = None
    if minha:
        minha_out = {
            'id': minha.id,
            'valor': float(minha.valor),
            'status': minha.status_,
            'data_criacao': minha.data_criacao.strftime('%d/%m/%Y %H:%M')
                if minha.data_criacao else None,
            'tempo_entrega_min': getattr(minha, 'tempo_entrega_min', None),
        }

    cliente_nome = _primeiro_nome(s.cliente.nome if s.cliente else '')

    # Bucket de distância (PII-safe).
    dist_bucket = None
    cli_obj = s.cliente
    if cli_obj is not None and cli_obj.latitude is not None and cli_obj.longitude is not None:
        cook = db.query(Cozinheiro).filter(Cozinheiro.id == cozinheiro_id).first()
        if cook is not None and cook.latitude is not None and cook.longitude is not None:
            d = distancia_km(cli_obj.latitude, cli_obj.longitude, cook.latitude, cook.longitude)
            dist_bucket = bucket_distancia_km(d)

    return {
        'id': s.id,
        'cliente_id': s.cliente_id,
        'cliente_nome': cliente_nome,
        'situacao': getattr(s, 'situacao', None) or 'aguardando_cozinheiro',
        'data': s.data_criacao.strftime('%d/%m/%Y') if s.data_criacao else '',
        'hora': s.data_criacao.strftime('%H:%M') if s.data_criacao else '',
        'criado_em_iso': s.data_criacao.isoformat() if s.data_criacao else None,
        'refeicoes_por_dia': s.refeicoes_por_dia,
        'calorias_diarias': s.calorias_diarias,
        'restricoes': s.restricoes,
        'alimentos_proibidos': s.alimentos_proibidos,
        'observacoes_nutricionista': s.observacoes_nutricionista,
        'qtd_dias': s.qtd_dias,
        'porcoes_por_refeicao': s.porcoes_por_refeicao,
        'observacoes_adicionais': s.observacoes_adicionais,
        'receita_link': rel,
        'ja_tem_proposta_minha': minha is not None,
        'minha_proposta': minha_out,
        'total_propostas': total_propostas,
        'cliente_distancia_bucket': dist_bucket,
    }


def _parse_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('1', 'true', 'yes', 'sim', 'on')


@app.route('/api/solicitacoes/abertas', methods=['GET'])
def listar_solicitacoes_abertas():
    """Lista solicitações em `aguardando_cozinheiro` para o cozinheiro logado.

    Query params (todos opcionais):
      - q: busca textual em restricoes, alimentos_proibidos, observacoes_*.
      - min_refeicoes / max_refeicoes: faixa de refeicoes_por_dia.
      - min_calorias / max_calorias: faixa de calorias_diarias.
      - somente_sem_proposta_minha: default 'true'. Oculta solicitações
        em que o próprio cozinheiro já enviou proposta (pendente).
      - limit (<=100, default 50), offset (default 0).
    """
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cozinheiro':
        return jsonify({'error': 'Não autorizado'}), 401

    cozinheiro_id = session['usuario_id']
    q = (request.args.get('q') or '').strip()
    min_ref = _safe_int(request.args.get('min_refeicoes'))
    max_ref = _safe_int(request.args.get('max_refeicoes'))
    min_cal = _safe_int(request.args.get('min_calorias'))
    max_cal = _safe_int(request.args.get('max_calorias'))
    somente_sem = _parse_bool(request.args.get('somente_sem_proposta_minha'), default=True)
    limit = _safe_int(request.args.get('limit'), default=50) or 50
    offset = _safe_int(request.args.get('offset'), default=0) or 0
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    db = SessionLocal()
    try:
        query = (
            db.query(Solicitacao)
            .filter(Solicitacao.situacao == 'aguardando_cozinheiro')
        )

        # Defesa-em-profundidade: exclui qualquer solicitação com proposta já aceita.
        aceitas_subq = (
            db.query(Proposta.solicitacao_id)
            .filter(Proposta.status_ == 1)
            .subquery()
        )
        query = query.filter(~Solicitacao.id.in_(aceitas_subq))

        if somente_sem:
            minhas_subq = (
                db.query(Proposta.solicitacao_id)
                .filter(Proposta.cozinheiro_id == cozinheiro_id)
                .subquery()
            )
            query = query.filter(~Solicitacao.id.in_(minhas_subq))

        if min_ref is not None:
            query = query.filter(Solicitacao.refeicoes_por_dia >= min_ref)
        if max_ref is not None:
            query = query.filter(Solicitacao.refeicoes_por_dia <= max_ref)
        if min_cal is not None:
            query = query.filter(Solicitacao.calorias_diarias >= min_cal)
        if max_cal is not None:
            query = query.filter(Solicitacao.calorias_diarias <= max_cal)

        if q:
            like = f'%{q}%'
            query = query.filter(or_(
                Solicitacao.restricoes.ilike(like),
                Solicitacao.alimentos_proibidos.ilike(like),
                Solicitacao.observacoes_nutricionista.ilike(like),
                Solicitacao.observacoes_adicionais.ilike(like),
            ))

        total = query.count()
        sols = (
            query.order_by(Solicitacao.data_criacao.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        out = [_serialize_solicitacao_aberta(s, db, cozinheiro_id) for s in sols]
        return jsonify({
            'solicitacoes': out,
            'total': total,
            'limit': limit,
            'offset': offset,
        })
    except Exception as e:
        print(f'Erro listar solicitacoes abertas: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/solicitacoes/<int:solicitacao_id>', methods=['GET'])
def obter_solicitacao(solicitacao_id):
    """Detalhe de uma solicitação.

    - Cozinheiro logado → view de descoberta (sem PII sensível), com
      `minha_proposta` quando já tiver respondido.
    - Cliente logado dono da solicitação → view completa (mesmo schema
      usado em `/api/cliente/home-pedidos`).
    """
    if 'usuario_id' not in session:
        return jsonify({'error': 'Não autorizado'}), 401

    tipo = session.get('usuario_tipo')
    uid = session['usuario_id']

    db = SessionLocal()
    try:
        s = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
        if not s:
            return jsonify({'error': 'Solicitação não encontrada'}), 404

        if tipo == 'cliente':
            if s.cliente_id != uid:
                return jsonify({'error': 'Não autorizado'}), 403
            return jsonify(_serialize_solicitacao_cliente(s, db))

        if tipo == 'cozinheiro':
            return jsonify(_serialize_solicitacao_aberta(s, db, uid))

        return jsonify({'error': 'Não autorizado'}), 401
    except Exception as e:
        print(f'Erro obter solicitacao: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/solicitacoes/<int:solicitacao_id>', methods=['DELETE'])
def delete_solicitacao(solicitacao_id):
    """Remove solicitação (e arquivo, propostas)."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    db = SessionLocal()
    try:
        s = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
        if not s or s.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Não encontrado'}), 404
        _unlink_receita_upload(s.receita_link or '')
        db.query(Proposta).filter(Proposta.solicitacao_id == s.id).delete()
        db.delete(s)
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/pedidos/<int:pedido_id>/cliente', methods=['DELETE'])
def delete_pedido_cliente(pedido_id):
    """Cliente remove pedido ativo.

    Regra (PLAN_USUARIO §13): bloqueia a remoção enquanto o pedido está
    em execução (`preparando` ou `saiu_entrega`). Nos demais estados —
    `confirmado` (ainda não iniciado), `entregue` e `cancelado` — o
    cliente pode limpar da lista.
    """
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    db = SessionLocal()
    try:
        p = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        if not p or p.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Pedido não encontrado'}), 404
        if (p.status or '') in ('preparando', 'saiu_entrega'):
            return jsonify({
                'success': False,
                'error': 'Pedido em preparo ou a caminho não pode ser cancelado.',
                'code': 'pedido_em_execucao',
            }), 409
        db.delete(p)
        db.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


# ============ API: CHECKOUT FAKE (PLAN_USUARIO §12) ============
_METODOS_PAGAMENTO_VALIDOS = {'pix', 'credito', 'debito'}


def _gerar_pix_copia_cola_fake(pedido) -> str:
    """Gera um EMV-ish determinístico para o pedido.

    Não é um BR Code válido — serve apenas para a UI copiar/colar e
    aparentar um PIX real no fluxo de demo.
    """
    import secrets
    token = secrets.token_hex(8).upper()
    valor = f"{float(pedido.val_total or 0):.2f}"
    return (
        f"00020126360014BR.GOV.BCB.PIX0114NUTRISYNC{pedido.id:06d}"
        f"5204000053039865406{valor}5802BR5913NutriSync FAKE"
        f"6009SAO PAULO62070503***6304{token[:4]}"
    )


def _serialize_pagamento(p) -> dict:
    return {
        'pedido_id': p.id,
        'status_pagamento': p.status_pagamento or 'pendente',
        'metodo_pagamento': p.metodo_pagamento,
        'pix_copia_cola': p.pix_copia_cola,
        'pagamento_data': p.pagamento_data.isoformat() if p.pagamento_data else None,
        'valor': float(p.val_total or 0),
    }


@app.route('/api/pedidos/<int:pedido_id>/pagamento', methods=['GET'])
def pagamento_status(pedido_id):
    """Snapshot do pagamento — usado pelo polling do checkout."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401
    db = SessionLocal()
    try:
        p = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        if not p or p.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Pedido não encontrado'}), 404
        return jsonify({'success': True, 'pagamento': _serialize_pagamento(p)})
    finally:
        db.close()


@app.route('/api/pedidos/<int:pedido_id>/pagamento/iniciar', methods=['POST'])
def pagamento_iniciar(pedido_id):
    """Prepara o pagamento fake (seleciona método e, se PIX, gera código).

    É idempotente para PIX: se já existe `pix_copia_cola` o valor é
    reaproveitado — permite ao cliente fechar e reabrir o checkout sem
    gerar um novo código.
    """
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    data = request.get_json(silent=True) or {}
    metodo = (data.get('metodo') or '').strip().lower()
    if metodo not in _METODOS_PAGAMENTO_VALIDOS:
        return jsonify({'success': False, 'error': 'Método inválido.'}), 400

    db = SessionLocal()
    try:
        p = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        if not p or p.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Pedido não encontrado'}), 404
        if (p.status_pagamento or 'pendente') == 'pago':
            return jsonify({'success': True, 'pagamento': _serialize_pagamento(p)})
        p.metodo_pagamento = metodo
        if metodo == 'pix' and not p.pix_copia_cola:
            p.pix_copia_cola = _gerar_pix_copia_cola_fake(p)
        db.commit()
        db.refresh(p)
        return jsonify({'success': True, 'pagamento': _serialize_pagamento(p)})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/pedidos/<int:pedido_id>/pagamento/confirmar', methods=['POST'])
def pagamento_confirmar(pedido_id):
    """Confirma o pagamento fake.

    Para cartão, exige os quatro campos básicos (número 13–19, validade
    MM/AA, CVV 3–4, titular) — só para validar a forma; nada é enviado
    a nenhum gateway. Para PIX, o botão "Já paguei" dispara esta mesma
    rota com `metodo='pix'` e sem `cartao`.
    """
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    data = request.get_json(silent=True) or {}
    metodo = (data.get('metodo') or '').strip().lower()
    if metodo not in _METODOS_PAGAMENTO_VALIDOS:
        return jsonify({'success': False, 'error': 'Método inválido.'}), 400

    if metodo in ('credito', 'debito'):
        cartao = data.get('cartao') or {}
        numero = ''.join(ch for ch in str(cartao.get('numero', '')) if ch.isdigit())
        cvv = ''.join(ch for ch in str(cartao.get('cvv', '')) if ch.isdigit())
        validade = str(cartao.get('validade', '')).strip()
        titular = str(cartao.get('titular', '')).strip()
        if not (13 <= len(numero) <= 19):
            return jsonify({'success': False, 'error': 'Número do cartão inválido.', 'field': 'numero'}), 400
        if not (3 <= len(cvv) <= 4):
            return jsonify({'success': False, 'error': 'CVV inválido.', 'field': 'cvv'}), 400
        # MM/AA — aceita com ou sem barra.
        v = validade.replace('/', '').replace(' ', '')
        if len(v) != 4 or not v.isdigit() or not (1 <= int(v[:2]) <= 12):
            return jsonify({'success': False, 'error': 'Validade inválida.', 'field': 'validade'}), 400
        if len(titular) < 2:
            return jsonify({'success': False, 'error': 'Informe o nome do titular.', 'field': 'titular'}), 400

    db = SessionLocal()
    try:
        p = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        if not p or p.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Pedido não encontrado'}), 404
        p.metodo_pagamento = metodo
        p.status_pagamento = 'pago'
        p.pagamento_data = datetime.now()
        db.commit()
        db.refresh(p)
        return jsonify({'success': True, 'pagamento': _serialize_pagamento(p)})
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


# ============ API: AVALIAR PEDIDO (alias do frontend) ============
@app.route('/api/pedidos/<int:pedido_id>/avaliar', methods=['POST'])
def avaliar_pedido(pedido_id):
    """Alias de `POST /api/avaliacoes` consumido pelo frontend.

    Aceita `{ avaliacao: 1..5 }` (nomenclatura do cliente RN) e
    delega para a lógica canônica via um mock mínimo de `request.json`.
    Mantém a média do cozinheiro consistente (a base é `Pedido.avaliacao`
    direto — cada pedido entregue entra com sua nota; média é recalculada
    em tempo real).
    """
    if 'usuario_id' not in session:
        return jsonify({'error': 'Usuário não logado'}), 401

    data = request.get_json(silent=True) or {}
    try:
        nota = int(data.get('avaliacao'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Nota inválida. Deve ser entre 1 e 5'}), 400
    if nota < 1 or nota > 5:
        return jsonify({'success': False, 'error': 'Nota inválida. Deve ser entre 1 e 5'}), 400

    db = SessionLocal()
    try:
        pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        if not pedido:
            return jsonify({'success': False, 'error': 'Pedido não encontrado'}), 404
        if session['usuario_id'] != pedido.cliente_id:
            return jsonify({'success': False, 'error': 'Não autorizado'}), 401
        if pedido.status != 'entregue':
            return jsonify({'success': False, 'error': 'Apenas pedidos entregues podem ser avaliados'}), 400
        if (pedido.avaliacao or 0) > 0:
            return jsonify({'success': False, 'error': 'Este pedido já foi avaliado'}), 400

        pedido.avaliacao = nota
        db.commit()

        from sqlalchemy import func as _f
        media = db.query(_f.avg(Pedido.avaliacao)).filter(
            Pedido.cozinheiro_id == pedido.cozinheiro_id,
            Pedido.avaliacao > 0,
        ).scalar() or 0
        cozinheiro = db.query(Cozinheiro).filter(Cozinheiro.id == pedido.cozinheiro_id).first()
        if cozinheiro:
            cozinheiro.avaliacao = int(round(float(media)))
            db.commit()
        total = db.query(Pedido).filter(
            Pedido.cozinheiro_id == pedido.cozinheiro_id,
            Pedido.avaliacao > 0,
        ).count()
        return jsonify({
            'success': True,
            'pedido_id': pedido.id,
            'nota': nota,
            'media_cozinheiro': float(media),
            'total_avaliacoes': total,
        })
    except Exception as e:
        db.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


# ============ ROTA PARA PÁGINA 404 ============
@app.errorhandler(404)
def pagina_nao_encontrada(e):
    """Página não encontrada"""
    return jsonify({'error': 'Página não encontrada'}), 404


# ============ ROTA PARA ERROS 500 ============
@app.errorhandler(500)
def erro_interno(e):
    """Erro interno do servidor"""
    return jsonify({'error': 'Erro interno do servidor'}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8000)