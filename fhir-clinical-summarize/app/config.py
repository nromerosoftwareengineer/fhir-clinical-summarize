from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, overridable with environment variables (e.g. FHIR_BASE_URL).

    Every value has a working local default, so `uvicorn app.main:app` needs no
    configuration on a developer laptop. In a container nothing is baked in: the
    deployment supplies the URLs, and the two auth scopes below are what switch
    the service from anonymous local servers to managed cloud endpoints.
    """

    model_config = SettingsConfigDict(env_file=".env")

    # -- FHIR -------------------------------------------------------------
    fhir_base_url: str = "http://localhost:8080/fhir"

    # Set this to turn on Entra ID (Azure AD) authentication against a managed
    # FHIR service, e.g.
    #   https://myws-myfhir.fhir.azurehealthcareapis.com/.default
    # When it is None the client sends no Authorization header, which is what a
    # local HAPI container expects. Requires the "azure" extra.
    fhir_auth_scope: str | None = None

    # The FHIR base URL a *browser* can reach, used only for the citation links
    # in the dashboard. In Azure the service often talks to FHIR over private
    # networking on an address no browser can resolve, so these differ. Leave it
    # unset to fall back to fhir_base_url.
    fhir_public_url: str | None = None

    # -- model ------------------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"

    # -- behaviour --------------------------------------------------------
    request_timeout_s: float = 60.0

    # JSON lines rather than human-readable text. Off locally, on in a
    # container, so Log Analytics / App Insights can parse the fields.
    log_json: bool = False
    log_level: str = "INFO"

    @property
    def browser_fhir_url(self) -> str:
        """The FHIR base the dashboard should link citations to."""
        return self.fhir_public_url or self.fhir_base_url


settings = Settings()
