from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, overridable with environment variables (e.g. FHIR_BASE_URL)."""

    model_config = SettingsConfigDict(env_file=".env")

    fhir_base_url: str = "http://localhost:8080/fhir"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    request_timeout_s: float = 60.0


settings = Settings()
