"""Entry point."""
import logging
import os
import sys

import config
from bot import TriageBot

log = logging.getLogger(__name__)


def main() -> None:
    level_name = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.DEBUG),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy 3rd-party loggers so our step-by-step logs are readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.INFO)

    log.info("[main] Starting bot (log level=%s)", level_name)

    log.info("[main] Validating environment variables...")
    missing = config.validate()
    if missing:
        log.error("[main] Missing required environment variables: %s", ", ".join(missing))
        print(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in values.",
            file=sys.stderr,
        )
        sys.exit(2)
    log.info(
        "[main] Config OK. monitored_channels=%s approval_channel=%s query_channel=%s model=%s team=%s min_conf=%.2f min_len=%d delay=%.1fs db=%s",
        config.MONITORED_CHANNEL_IDS,
        config.APPROVAL_CHANNEL_ID,
        config.QUERY_CHANNEL_ID or f"{config.query_channel_id()} (approval fallback)",
        config.CLASSIFIER_MODEL,
        config.LINEAR_TEAM_ID,
        config.MIN_CONFIDENCE,
        config.MIN_MESSAGE_LENGTH,
        config.CLASSIFY_DELAY_SECONDS,
        config.DB_PATH,
    )

    log.info("[main] Constructing TriageBot...")
    bot = TriageBot()
    log.info("[main] Connecting to Discord gateway...")
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
