"""Network error noise control and a global error handler for the bot.

Telegram's API regularly answers with 502/timeout for a few seconds. python-telegram-bot
retries those on its own, but logs every attempt as ERROR with a full traceback. That is
pure noise for an alerting pipeline that treats ERROR as "wake me up".

What this module does:

* Transient network failures are rewritten to a single-line WARNING (no traceback).
* A watchdog escalates to a real ERROR only when the API stays unreachable for
  OUTAGE_ALERT_AFTER seconds - i.e. when the bot really is not working - and logs
  another ERROR once connectivity comes back.
* Everything that is NOT a transient network problem (bad token, Conflict, bugs in
  handlers) keeps its ERROR level and traceback.
"""

import asyncio
import logging
import time

from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError

# --- Tunables (seconds) ------------------------------------------------------
OUTAGE_ALERT_AFTER = 300  # keep quiet for this long, then alert: the outage is real
OUTAGE_REALERT_EVERY = 1800  # remind every 30 min while still down
RECOVERY_QUIET_PERIOD = 90  # no failure for this long == connectivity is back
# Invariant: RECOVERY_QUIET_PERIOD must stay well below OUTAGE_ALERT_AFTER, otherwise a
# short blip can cross the alert threshold while we are still waiting to call it
# recovered. The watchdog clamps it rather than trusting the constants above.
WATCHDOG_INTERVAL = 15  # how often the watchdog re-evaluates

# Loggers python-telegram-bot uses for network complaints.
NOISY_LOGGERS = (
    "telegram.ext.Updater",
    "telegram.ext.Application",
    "telegram.request.HTTPXRequest",
)

logger = logging.getLogger("network")


def is_transient(exc):
    """True for errors that resolve themselves on retry.

    NetworkError covers 502/Bad Gateway, timeouts and DNS hiccups. BadRequest subclasses
    NetworkError in PTB but means "we sent something wrong", so it is excluded, as are
    InvalidToken and Conflict (another instance is polling) - both need a human.
    """
    if isinstance(exc, (BadRequest, InvalidToken, Conflict)):
        return False
    return isinstance(exc, NetworkError)


class NetworkHealth:
    """Tracks the current run of consecutive network failures."""

    def __init__(self):
        self.first_failure = None
        self.last_failure = None
        self.failures = 0
        self.last_error = ""
        self.alerted_at = None

    def record_failure(self, exc):
        now = time.monotonic()
        if self.first_failure is None:
            self.first_failure = now
        self.last_failure = now
        self.failures += 1
        self.last_error = "{}: {}".format(type(exc).__name__, exc)
        return self.failures

    def reset(self):
        self.first_failure = None
        self.last_failure = None
        self.failures = 0
        self.last_error = ""
        self.alerted_at = None


class TransientNetworkFilter(logging.Filter):
    """Downgrades retryable network errors to a short WARNING and drops the traceback."""

    def __init__(self, health):
        super().__init__()
        self.health = health

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        if not is_transient(exc):
            return True

        count = self.health.record_failure(exc)
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        record.msg = "Telegram network hiccup (%s: %s) - retrying, failure #%d in this run"
        record.args = (type(exc).__name__, exc, count)
        record.exc_info = None
        record.exc_text = None
        return True


def install_network_error_filter(logger_names=NOISY_LOGGERS):
    """Attach the filter and return the shared NetworkHealth instance."""
    health = NetworkHealth()
    noise_filter = TransientNetworkFilter(health)
    for name in logger_names:
        logging.getLogger(name).addFilter(noise_filter)
    return health


def _humanize(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m {}s".format(seconds // 60, seconds % 60)
    return "{}h {}m".format(seconds // 3600, (seconds % 3600) // 60)


async def network_watchdog(health):
    """Turn a sustained run of network failures into exactly one ERROR alert."""
    quiet_period = min(RECOVERY_QUIET_PERIOD, OUTAGE_ALERT_AFTER / 2.0)
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        if health.last_failure is None:
            continue

        now = time.monotonic()
        down_for = now - health.first_failure
        quiet_for = now - health.last_failure

        if quiet_for >= quiet_period:
            if health.alerted_at is not None:
                logger.error(
                    "RECOVERED: Telegram API reachable again after %s and %d failed attempts. "
                    "The bot is receiving updates.",
                    _humanize(health.last_failure - health.first_failure),
                    health.failures,
                )
            else:
                logger.info(
                    "Telegram network settled after %d retries.", health.failures
                )
            health.reset()
        elif health.alerted_at is None and down_for >= OUTAGE_ALERT_AFTER:
            logger.error(
                "Telegram API unreachable for %s (%d consecutive failures, last error: %s). "
                "The bot is NOT receiving messages and support replies are NOT delivered.",
                _humanize(down_for),
                health.failures,
                health.last_error,
            )
            health.alerted_at = now
        elif (
            health.alerted_at is not None
            and now - health.alerted_at >= OUTAGE_REALERT_EVERY
        ):
            logger.error(
                "Telegram API STILL unreachable, now %s and %d failures (last error: %s).",
                _humanize(down_for),
                health.failures,
                health.last_error,
            )
            health.alerted_at = now


async def error_handler(update, context):
    """Catch-all for exceptions raised inside handlers."""
    error = context.error
    if is_transient(error):
        logger.warning(
            "Telegram network hiccup while handling an update (%s: %s) - message may be lost",
            type(error).__name__,
            error,
        )
        return

    logger.error(
        "Unhandled exception while processing update %s",
        getattr(update, "update_id", update),
        exc_info=error,
    )
