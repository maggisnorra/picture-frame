# picture-frame
Web controlled Full HD digital picture frame with video call functionality.

## Cloud API

Setup:
```
python -m venv .venv && source .venv/bin/activate
pip install "fastapi>=0.115" "uvicorn[standard]" "SQLAlchemy>=2.0" pydantic pydantic-settings alembic "psycopg[binary]"
```

Run:
```
uvicorn main:app --reload --port 8000
```

