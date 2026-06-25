# CypherCrew Deployment

## Local setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run.py
```

Open `http://127.0.0.1:5000`

Default login is controlled from `.env`:

- Email: `DEFAULT_ADMIN_EMAIL`
- Password: `DEFAULT_ADMIN_PASSWORD`

## Production run

```bash
pip install -r requirements.txt
cp .env.example .env
# edit SECRET_KEY and DATABASE_URL
python -m gunicorn wsgi:app -b 0.0.0.0:8000
```

For HTTPS deployment, set:

```env
SESSION_COOKIE_SECURE=True
REMEMBER_COOKIE_SECURE=True
```
