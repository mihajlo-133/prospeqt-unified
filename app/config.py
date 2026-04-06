from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    admin_password: str = "changeme"
    secret_key: str = "dev-secret-change-in-prod"
    poll_interval_minutes: int = 15
    request_timeout: int = 15

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
