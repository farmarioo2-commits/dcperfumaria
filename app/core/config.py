from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./gestaofacil.db"
    jwt_secret: str = "troque-esta-chave"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
