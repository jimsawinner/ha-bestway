"""Constants for the bestway integration."""

from enum import Enum, StrEnum

DOMAIN = "bestway"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_API_ROOT = "apiroot"
CONF_API_ROOT_EU = "https://euapi.gizwits.com"
CONF_API_ROOT_US = "https://usapi.gizwits.com"
CONF_USER_TOKEN = "user_token"
CONF_USER_TOKEN_EXPIRY = "user_token_expiry"
CONF_UID = "uid"
GIZWITS_APP_ID = "98754e684ec045528b073876c34c7348"


class Backend(StrEnum):
    """Cloud backend selected at config flow time."""

    GIZWITS = "gizwits"
    AWS_IOT = "aws_iot"
    SMARTSPA = "smartspa"


CONF_BACKEND = "backend"

CONF_VISITOR_ID = "visitor_id"
CONF_TOKEN = "token"
CONF_LOCATION = "location"  # sent to the AWS IoT auth endpoint, e.g. "GB"
CONF_REGION = "region"
CONF_API_BASE = "api_base"

CONF_SMARTSPA_ACCOUNT = "smartspa_account"
CONF_SMARTSPA_PASSWORD = "smartspa_password"
CONF_SMARTSPA_REGION = "smartspa_region"
CONF_SMARTSPA_TOKEN = "smartspa_token"

# Bubble UI mode (Airjet V02). Some V02 hardware (e.g. T53NN8 batches)
# only has on/off bubbles physically, while others support 3 levels.
# The product_id doesn't distinguish them, so the user picks.
CONF_BUBBLES_MODE = "bubbles_mode"
BUBBLES_MODE_3WAY = "three_way"
BUBBLES_MODE_ONOFF = "on_off"
BUBBLES_MODE_DEFAULT = BUBBLES_MODE_3WAY

# Assumed per-component draw used by the estimated power/energy sensors.
# Real draw varies by model, so users can correct the defaults.
CONF_HEATER_WATTS = "heater_watts"
CONF_BUBBLES_WATTS = "bubbles_watts"
CONF_FILTER_WATTS = "filter_watts"
CONF_JETS_WATTS = "jets_watts"

# Seconds between polls when no WebSocket is delivering updates - always the
# case on the SmartSpa backend. Shorter intervals show changes made outside
# Home Assistant sooner, at the cost of more requests to Bestway's cloud.
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_INTERVAL = 30
MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 300


class Icon(str, Enum):
    """Icon styles."""

    BUBBLES = "mdi:chart-bubble"
    FILTER = "mdi:image-filter-tilt-shift"
    HARDWARE = "mdi:chip"
    JETS = "mdi:turbine"
    LOCK = "mdi:lock"
    POWER = "mdi:power"
    PROTOCOL = "mdi:protocol"
    SOFTWARE = "mdi:application-braces"
