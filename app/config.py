from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ozon_client_id: str = ""
    ozon_api_key: str = ""
    ozon_cookie: str = ""
    ozon_base_url: str = "https://seller.ozon.ru"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    database_url: str = "sqlite:///./reviews.db"
    poll_interval_seconds: int = 300
    auto_post_enabled: bool = True
    auto_post_negative_enabled: bool = False
    initial_pages: int = 5        # страниц при первом запуске (5 = 500 новейших отзывов)
    poll_pages: int = 1           # страниц при регулярном опросе

    host: str = "0.0.0.0"
    port: int = 8000

    secret_key: str = "change-me-in-production"
    app_username: str = "admin"
    app_password: str = "password"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
