import uvicorn

from app.config import load_config


def main() -> None:
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
