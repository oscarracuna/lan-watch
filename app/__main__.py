import uvicorn

from app.config import load_config
from app.notify import load_secrets


def main() -> None:
    load_secrets()
    settings = load_config()
    uvicorn.run(
        "app.main:app",
        host=settings.listen,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
