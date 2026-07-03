"""
Central configuration loaded from environment variables / .env file.
"""
from enum import Enum
from typing import Optional

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HTJ2KMode(str, Enum):
    LOSSLESS = "lossless"           # TS 1.2.840.10008.1.2.4.202
    LOSSLESS_RPCL = "lossless_rpcl" # TS 1.2.840.10008.1.2.4.203
    LOSSY = "lossy"                 # TS 1.2.840.10008.1.2.4.204


class CloudPACSAuthScheme(str, Enum):
    NONE = "none"
    BASIC = "basic"
    BEARER = "bearer"
    OAUTH2 = "oauth2"


# Transfer Syntax UIDs for HTJ2K
HTJ2K_TRANSFER_SYNTAXES: dict[HTJ2KMode, str] = {
    HTJ2KMode.LOSSLESS:      "1.2.840.10008.1.2.4.202",
    HTJ2KMode.LOSSLESS_RPCL: "1.2.840.10008.1.2.4.203",
    HTJ2KMode.LOSSY:         "1.2.840.10008.1.2.4.204",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------- DICOMweb proxy listener -------
    proxy_host: str = Field("0.0.0.0", description="Host for the DICOMweb proxy to bind on")
    proxy_port: int = Field(8080, description="Port for the DICOMweb proxy")
    proxy_base_path: str = Field("/wado-rs", description="Base path for STOW-RS endpoint")

    # ------- DIMSE listener (C-STORE SCP) -------
    dimse_host: str = Field("0.0.0.0", description="Host for the C-STORE SCP")
    dimse_port: int = Field(11112, description="Port for the C-STORE SCP")
    dimse_ae_title: str = Field("HTJ2K_PROXY", description="AE title of the SCP")

    # ------- Cloud PACs target -------
    cloud_pacs_url: AnyHttpUrl = Field(..., description="Base URL of the cloud PACs DICOMweb endpoint (e.g. https://pacs.example.com/wado)")
    cloud_pacs_ae_title: Optional[str] = Field(None, description="AE title for DIMSE cloud PACs target")
    cloud_pacs_dimse_host: Optional[str] = Field(None, description="Host of cloud PACs DIMSE endpoint")
    cloud_pacs_dimse_port: Optional[int] = Field(None, description="Port of cloud PACs DIMSE endpoint")

    # ------- Cloud PACs authentication -------
    cloud_pacs_auth_scheme: CloudPACSAuthScheme = Field(CloudPACSAuthScheme.NONE)
    cloud_pacs_username: Optional[str] = None
    cloud_pacs_password: Optional[str] = None
    cloud_pacs_token: Optional[str] = None

    # OAuth2 client-credentials
    cloud_pacs_oauth2_token_url: Optional[str] = None
    cloud_pacs_oauth2_client_id: Optional[str] = None
    cloud_pacs_oauth2_client_secret: Optional[str] = None

    # ------- Compression options -------
    htj2k_mode: HTJ2KMode = Field(HTJ2KMode.LOSSLESS, description="HTJ2K compression mode")
    htj2k_lossy_quality: float = Field(
        40.0,
        ge=1.0,
        le=1000.0,
        description="Peak signal-to-noise ratio (dB) target for lossy mode",
    )
    htj2k_num_threads: int = Field(0, description="0 = use all CPU cores")

    # ------- HTTP client tuning -------
    http_timeout_seconds: float = Field(120.0)
    http_max_retries: int = Field(3)


settings = Settings()
