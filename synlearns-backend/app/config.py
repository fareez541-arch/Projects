from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    db_name: str = "synlearns"
    db_user: str = "sls"
    db_password: str = ""
    db_host: str = "sls-db"
    db_port: int = 5432

    # JWT
    jwt_private_key_path: str = "/app/keys/jwt_private.pem"
    jwt_public_key_path: str = "/app/keys/jwt_public.pem"
    jwt_algorithm: str = "RS256"
    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 7

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""  # deprecated — use tier-specific IDs below
    stripe_price_id_feedback: str = ""  # $79 Feedback Cohort → tier 1
    stripe_price_id_referral: str = ""  # $119 Referral → tier 2
    stripe_price_id_full: str = ""      # $149 Full Access → tier 3

    # Content encryption
    content_encryption_key: str = ""

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "info@synlearns.ai"
    smtp_from_name: str = "Synaptic Learning Systems"

    # App
    app_url: str = "https://synlearns.ai"
    api_url: str = "https://api.synlearns.ai"
    admin_email: str = "dev@synlearns.ai"

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def database_url_sync(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
