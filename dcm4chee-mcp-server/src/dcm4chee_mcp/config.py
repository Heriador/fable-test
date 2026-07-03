"""Environment-driven configuration for the DCM4CHEE MCP server."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings can be provided via environment variables or a .env file."""

    model_config = SettingsConfigDict(
        env_prefix="DCM4CHEE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- dcm4chee-arc REST API ---
    # Base URL of the archive, e.g. http://localhost:8080/dcm4chee-arc
    base_url: str = "http://localhost:8080/dcm4chee-arc"
    # Archive AE title used for DICOMweb / echo requests
    aet: str = "DCM4CHEE"
    http_timeout_seconds: float = 30.0

    # --- Keycloak auth (secured deployments only; leave unset for unsecured) ---
    keycloak_token_url: str | None = None  # e.g. https://host:8843/realms/dcm4che/protocol/openid-connect/token
    keycloak_client_id: str | None = None
    keycloak_client_secret: str | None = None
    keycloak_verify_tls: bool = True

    # --- Log access ---
    # Path to the WildFly server.log (typically a docker volume mount, e.g.
    # /var/local/dcm4chee-arc/wildfly/standalone/log/server.log)
    log_file_path: Path = Path("/var/local/dcm4chee-arc/wildfly/standalone/log/server.log")
    # Optional: docker container name for `docker logs` fallback when the
    # log file is not readable from this process.
    docker_container: str | None = None
    # Hard cap on lines returned by log tools to protect the model context.
    max_log_lines: int = 500

    @property
    def auth_enabled(self) -> bool:
        return bool(self.keycloak_token_url and self.keycloak_client_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
