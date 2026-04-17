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
from demo_constants import (
    DEMO_BASE_VALOR,
    DEMO_COZINHEIRO_NOME,
    ensure_demo_cozinheiro,
    demo_opciones_json,
    demo_proposta_extras_json,
    demo_valor_total_com_opcao,
    is_demo_cozinheiro_email,
)

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
    """Retorna lista de cozinheiros para o marketplace"""
    db = SessionLocal()
    try:
        especialidade_filtro = request.args.get('especialidade')
        
        query = db.query(Cozinheiro)
        if especialidade_filtro:
            query = query.join(Especialidade).filter(Especialidade.nome == especialidade_filtro)
        
        cozinheiros = query.all()
        
        resultado = []
        for c in cozinheiros:
            # Calcular média de avaliações
            from sqlalchemy import func
            media_avaliacoes = db.query(func.avg(Pedido.avaliacao)).filter(
                Pedido.cozinheiro_id == c.id,
                Pedido.avaliacao > 0
            ).scalar() or 0
            
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
                'tipo_entrega': c.tipo_entrega
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
                'pode_avaliar': p.status == 'entregue' and p.avaliacao == 0
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
            })

        return jsonify(resultado)
    finally:
        db.close()


# ============ API: PEDIDOS DO COZINHEIRO (ATUALIZADO COM PROPOSTA) ============
@app.route('/api/pedidos/cozinheiro/<int:cozinheiro_id>', methods=['GET'])
def pedidos_do_cozinheiro(cozinheiro_id):
    """Retorna os pedidos de um cozinheiro para o painel"""
    if 'usuario_id' not in session or session['usuario_id'] != cozinheiro_id:
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
                'endereco_entrega': f"{p.cliente.rua}, {p.cliente.numero} - {p.cliente.complemento if p.cliente.complemento else ''}".strip()
            })
        
        return jsonify(resultado)
    finally:
        db.close()
        
# ============ API: ATUALIZAR STATUS DO PEDIDO ============
@app.route('/api/pedidos/<int:pedido_id>/status', methods=['PUT'])
def atualizar_status_pedido(pedido_id):
    """Atualiza o status de um pedido"""
    db = SessionLocal()
    try:
        data = request.json
        status = data.get('status')
        
        pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        
        if not pedido:
            return jsonify({'error': 'Pedido não encontrado'}), 404
        
        if session.get('usuario_tipo') != 'cozinheiro' or session['usuario_id'] != pedido.cozinheiro_id:
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
                # Verificar se especialidade existe
                especialidade = db.query(Especialidade).filter(Especialidade.id == data['especialidade_id']).first()
                if especialidade:
                    usuario.especialidade_id = especialidade.id
            
            # Atualizar senha se fornecida
            if 'senha' in data and data['senha']:
                valido, senha_hash = validar_senha(data['senha'])
                if not valido:
                    return jsonify({'error': senha_hash}), 400
                usuario.senha = senha_hash
            
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
    """Cria uma nova proposta para uma receita"""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cozinheiro':
        return jsonify({'error': 'Não autorizado'}), 401
    
    db = SessionLocal()
    try:
        data = request.json
        
        #nova_proposta = Proposta(
        #    valor=Decimal(str(data['valor'])),
        #    cozinheiro_id=session['usuario_id'],
        #    data_criacao=datetime.now(),
        #    receita_link=data.get('receita_link')
        #)
        nova_proposta = Proposta(
            valor=Decimal(str(data['valor'])),
            cozinheiro_id=session['usuario_id'],
            solicitacao_id=data['solicitacao_id'],  # OBRIGATÓRIO AGORA
            status_=0,
            data_criacao=datetime.now()
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
                'data_criacao': nova_proposta.data_criacao.strftime('%d/%m/%Y %H:%M'),
                'receita_link': nova_proposta.solicitacao.receita_link if nova_proposta.solicitacao else None
            }
        })
    except Exception as e:
        db.rollback()
        print(f"Erro ao criar proposta: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ============ API: LISTAR PROPOSTAS DO COZINHEIRO ============
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
    }


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
        proposta_pendente = {
            'id': pend.id,
            'valor': float(pend.valor),
            'cozinheiro_nome': c.nome if c else 'Cozinheiro',
            'tipo_entrega': (c.tipo_entrega or 'Combinar retirada ou entrega') if c else '',
        }
        if c and is_demo_cozinheiro_email(getattr(c, 'email', None)):
            proposta_pendente['base_valor'] = float(DEMO_BASE_VALOR)
            proposta_pendente['opciones_entrega'] = demo_opciones_json(s.id)
            proposta_pendente['es_demo'] = True
            proposta_pendente['tipo_entrega'] = 'Escolha uma opção de entrega abaixo.'
            cli_sol = db.query(Cliente).filter(Cliente.id == s.cliente_id).first()
            proposta_pendente.update(demo_proposta_extras_json(cli_sol))
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


@app.route('/api/solicitacoes/<int:solicitacao_id>/demo-proposta', methods=['POST'])
def solicitacao_demo_proposta(solicitacao_id):
    """Demo: cria proposta de um cozinheiro para a solicitação (uma vez)."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    db = SessionLocal()
    try:
        cli = db.query(Cliente).filter(Cliente.id == session['usuario_id']).first()
        s = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
        if not s or s.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Solicitação não encontrada'}), 404
        if getattr(s, 'situacao', '') == 'convertida':
            return jsonify({
                'success': False,
                'error': 'Solicitação já convertida',
                'error_code': 'SOLICITACAO_CONVERTIDA',
            }), 400
        if getattr(s, 'demo_convite_recusado', 0):
            return jsonify({
                'success': False,
                'error': 'Convite não disponível',
                'error_code': 'DEMO_CONVITE_INVALIDO',
            }), 400
        if s.situacao not in ('aguardando_cozinheiro',):
            has_pend = (
                db.query(Proposta)
                .filter(Proposta.solicitacao_id == s.id, Proposta.status_ == 0)
                .first()
            )
            if has_pend:
                p = has_pend
                c = db.query(Cozinheiro).filter(Cozinheiro.id == p.cozinheiro_id).first()
                out = {
                    'success': True,
                    'ja_existia': True,
                    'cozinheiro_nome': c.nome if c else '',
                    'valor': float(p.valor),
                    'tipo_entrega': (c.tipo_entrega or 'Combinar retirada ou entrega') if c else '',
                    'proposta_id': p.id,
                }
                if c and is_demo_cozinheiro_email(getattr(c, 'email', None)):
                    out['base_valor'] = float(DEMO_BASE_VALOR)
                    out['opciones_entrega'] = demo_opciones_json(s.id)
                    out['es_demo'] = True
                    out.update(demo_proposta_extras_json(cli))
                return jsonify(out)

        cozinheiro = ensure_demo_cozinheiro(db)

        prop = Proposta(
            valor=DEMO_BASE_VALOR,
            cozinheiro_id=cozinheiro.id,
            solicitacao_id=s.id,
            status_=0,
            data_criacao=datetime.now(),
        )
        db.add(prop)
        s.situacao = 'aguardando_cliente'
        db.commit()
        db.refresh(prop)
        payload = {
            'success': True,
            'ja_existia': False,
            'cozinheiro_nome': DEMO_COZINHEIRO_NOME,
            'base_valor': float(DEMO_BASE_VALOR),
            'valor': float(prop.valor),
            'tipo_entrega': 'Escolha uma opção de entrega abaixo.',
            'opciones_entrega': demo_opciones_json(s.id),
            'es_demo': True,
            'proposta_id': prop.id,
        }
        payload.update(demo_proposta_extras_json(cli))
        return jsonify(payload)
    except Exception as e:
        db.rollback()
        print(f'Erro demo-proposta: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/propostas/<int:proposta_id>/responder-cliente', methods=['POST'])
def proposta_responder_cliente(proposta_id):
    """Cliente aceita ou recusa proposta (demo). Aceitar cria Pedido."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    data = request.get_json(silent=True) or {}
    aceitar = bool(data.get('aceitar'))
    demo_entrega_opcao = (data.get('demo_entrega_opcao') or '').strip()

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
            if coz and is_demo_cozinheiro_email(getattr(coz, 'email', None)):
                total = demo_valor_total_com_opcao(demo_entrega_opcao, sol.id)
                if total is None:
                    return jsonify({
                        'success': False,
                        'error': 'Escolha uma opção de entrega válida.',
                        'error_code': 'DEMO_ENTREGA_INVALIDA',
                    }), 400
                prop.valor = total
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
                val_total=prop.valor,
                marmita_id=marmita.id if marmita else None,
                proposta_id=prop.id,
                plano_id=plano_id,
                avaliacao=0,
            )
            db.add(pedido)
            prop.status_ = 1
            prop.data_aceita = datetime.now()
            sol.situacao = 'convertida'
        else:
            prop.status_ = 2
            sol.situacao = 'recusada_cliente'
            sol.demo_convite_recusado = 1

        db.commit()
        return jsonify({'success': True, 'aceitar': aceitar})
    except Exception as e:
        db.rollback()
        print(f'Erro responder proposta: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
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
    """Cliente remove pedido ativo."""
    if 'usuario_id' not in session or session.get('usuario_tipo') != 'cliente':
        return jsonify({'success': False, 'error': 'Não autorizado'}), 401

    db = SessionLocal()
    try:
        p = db.query(Pedido).filter(Pedido.id == pedido_id).first()
        if not p or p.cliente_id != session['usuario_id']:
            return jsonify({'success': False, 'error': 'Pedido não encontrado'}), 404
        db.delete(p)
        db.commit()
        return jsonify({'success': True})
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