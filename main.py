"""Application entrypoint — starts FastAPI with uvicorn."""

import os
import uvicorn


def main() -> None:
    """Start the application — uvicorn handles both webhook and polling modes."""
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "app.bot.webhook:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
