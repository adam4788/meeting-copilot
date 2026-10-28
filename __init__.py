"""Plugin entrypoint: this package exposes no tools or hooks.

The desktop pane (desktop/plugin.js) and the dashboard API
(dashboard/plugin_api.py) are the live surfaces; capture never starts at
import or registration.
"""


def register(ctx):
    return None
