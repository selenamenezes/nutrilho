from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

# Resolve .env next to this file so cwd does not matter (e.g. running from repo root)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

_db_user = os.getenv("DB_USER") or ""
_db_password = os.getenv("DB_PASSWORD") or ""
_db_host = os.getenv("DB_HOST") or "localhost"
_db_port = os.getenv("DB_PORT") or "3306"
_db_name = os.getenv("DB_NAME") or ""

# Passwords with @, :, /, etc. must be URL-encoded or the URL breaks at the first "@"
DATABASE_URL = (
    f"mysql+pymysql://{quote_plus(_db_user)}:{quote_plus(_db_password)}"
    f"@{_db_host}:{_db_port}/{_db_name}"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Adicionar 'db' para compatibilidade com seus models
db = SessionLocal()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()