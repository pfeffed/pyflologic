"""Constants for FloLogic's official Web API.

FloLogic documents the API at http://developers.myflologic.com/ and publishes a
Swagger UI at https://api.myflologic.com/swagger. The documentation labels the
base URL "staging"; it is the only one published.
"""

from __future__ import annotations

DEFAULT_BASE_URL = "https://api.myflologic.com"
"""The Web API root, labelled staging in FloLogic's documentation."""

DEFAULT_AUTHORIZE_URL = "https://dashboard.myflologic.com/dashboard"
"""The consent page a user is sent to in the OAuth2 authorization-code flow."""

# --- endpoints ---------------------------------------------------------------

PATH_TOKEN = "/Token"
PATH_OAUTH_TOKEN = "/oauth/token"
PATH_SYSTEM_ALL = "/System/All"
PATH_SYSTEM_STATUS = "/System/Status"
PATH_SYSTEM_VALVE_MODE = "/System/ValveMode"
PATH_SYSTEM_FLOW_SENSITIVITY = "/System/FlowSensitivity"
PATH_USER_ALL = "/User/All"
PATH_USER_STATUS = "/User/Status"
PATH_CALLBACKS = "/Callbacks"

# --- request limits ----------------------------------------------------------

MAX_VIDS_PER_REQUEST = 10
"""Most VIDs one ``vid_list`` may carry, per the documentation."""

MAX_PAGE_SIZE = 100
"""Largest ``pageSize`` the paginated endpoints accept."""

# --- timing ------------------------------------------------------------------

DEFAULT_REQUEST_TIMEOUT = 30.0
"""Seconds to wait for any one HTTP request."""

TOKEN_REFRESH_MARGIN = 300.0
"""Renew an access token this many seconds before it expires.

A token that expires between being read and being presented costs a 401 and a
retry; renewing early makes that a rarity rather than a daily event.
"""

TOKEN_LIFETIME = 24 * 60 * 60.0
"""Documented lifetime of both kinds of access token.

Only used when a token carries no readable ``exp`` claim.
"""

MIN_POLL_INTERVAL = 30.0
"""Smallest polling interval callers should use against the Web API.

FloLogic documents no rate limit -- it defers to "your API key documentation",
and none came with the key. This library never polls on its own; the floor is
advice for the caller, and errs on the side of not finding the limit by
exceeding it.
"""

DEFAULT_POLL_INTERVAL = 60.0
"""Suggested polling interval when callbacks are not delivering events."""

# --- callbacks (webhooks) ----------------------------------------------------

HEADER_TIMESTAMP = "X-FloLogic-Timestamp"
HEADER_SIGNATURE = "X-FloLogic-Signature"

SIGNATURE_PREFIX = "sha256="

SIGNATURE_TOLERANCE = 300.0
"""Seconds a signed callback stays valid, per FloLogic's guidance."""
