from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..config import ENV_FILE
from ..db import DEFAULT_PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    # The prospectus tables share the main database so an analysis can join to its ISIN.
    rhp_database_path: Path = DEFAULT_PATH
    rhp_default_model: str = "gpt-5.6-terra"
    rhp_default_retries: int = Field(default=2, ge=0, le=5)
    rhp_max_pdf_bytes: int = Field(default=50_000_000, ge=1_000_000, le=500_000_000)
    rhp_section_concurrency: int = Field(default=4, ge=1, le=10)
    rhp_job_concurrency: int = Field(default=1, ge=1, le=10)
    rhp_api_tokens: SecretStr | None = None
    ipo_allotment_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    ipo_allotment_catalogue_ttl_seconds: int = Field(default=300, ge=0, le=3600)
    # An HTTP proxy used only to fetch KFintech's issue list. Their CDN can cache an error at one
    # edge, and a proxy exiting elsewhere reaches a good copy. PAN lookups never use it.
    kfintech_proxy_url: str | None = None
    # The same for Purva, but for every request: it answers 403 to cloud provider addresses.
    # HTTPS is tunnelled, so the proxy never sees a PAN.
    purva_proxy_url: str | None = None

    def api_tokens(self) -> set[str]:
        if self.rhp_api_tokens is None:
            return set()
        return {
            token.strip()
            for token in self.rhp_api_tokens.get_secret_value().split(",")
            if token.strip()
        }

    @property
    def analysis_enabled(self) -> bool:
        """Whether this deployment accepts new analyses: bearer tokens and an OpenAI key are both configured."""
        return bool(self.api_tokens()) and self.openai_api_key is not None and bool(
            self.openai_api_key.get_secret_value()
        )

    def require_openai_api_key(self) -> str:
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value():
            raise RuntimeError("OPENAI_API_KEY is not set.")
        return self.openai_api_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
