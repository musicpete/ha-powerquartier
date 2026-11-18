"""Constants for the Powerquartier integration."""

DOMAIN = "powerquartier"

CONF_PROFILES = "profiles"  # list of {"account_id", "name"}
CONF_DAYS_BACK = "days_back"
DEFAULT_DAYS_BACK = 30

# Energy refresh runs at these local times (portal data lags 1-2 days and
# arrives during the day); the Stromampel/tariff refresh runs every 30 min.
IMPORT_TIMES = ((7, 17), (19, 17))
AMPEL_INTERVAL_MIN = 30
STARTUP_DELAY_S = 120

SERVICE_REFRESH = "refresh"
ATTR_DAYS = "days"
ATTR_PROFILE = "profile"

SIGNAL_IMPORT_UPDATED = f"{DOMAIN}_import_updated"

# A failed run (portal unreachable, repeated errors) is retried this soon,
# at most this many times before waiting for the next scheduled run.
RETRY_AFTER_FAILURE_MIN = 30
RETRY_MAX_PER_CYCLE = 3
