from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/finance.db"
    admin_username: str = "admin"
    admin_password: str = ""
    cookie_secure: bool = False
    allow_registration: bool = False
    session_days: int = 14
    max_upload_mb: int = 10
    # Signs the short-lived cookie used during the OIDC login round trip. A random key is
    # generated at startup when empty, which only means a login in progress fails after a restart.
    secret_key: str = ""
    # Public base URL (e.g. https://ledger.example.com), used to build the OIDC redirect URI.
    # When empty it is derived from the request, which needs correct X-Forwarded-* headers.
    public_url: str = ""
    password_login: bool = True

    # Single sign-on via OpenID Connect (authentik, Keycloak, Zitadel, Authelia, ...).
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_display_name: str = "SSO"
    oidc_scopes: str = "openid email profile"
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    oidc_admin_group: str = ""  # members become admins; when empty, admin rights are managed in the app
    oidc_link_existing_users: bool = False  # attach SSO logins to local users with the same username

    # Bank sync via FinTS/HBCI. The product ID is the registration number from the
    # Deutsche Kreditwirtschaft (free, apply at https://www.fints.org).
    fints_product_id: str = ""
    bank_sync_interval_hours: int = 6  # automatic sync for connections with a stored PIN; 0 = off

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer_url and self.oidc_client_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
