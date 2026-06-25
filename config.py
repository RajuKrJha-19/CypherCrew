import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")

    SQLALCHEMY_DATABASE_URI = "sqlite:///cyphercrew.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "False").lower() == "true"
    REMEMBER_COOKIE_SECURE = os.getenv("REMEMBER_COOKIE_SECURE", "False").lower() == "true"

    AUTO_SEED = os.getenv("AUTO_SEED", "True").lower() == "true"
