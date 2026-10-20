"""Client Resizing default under Wine (Mac client).

On Wine the Client Resizing tick rewrites game memory and window styles every 0.5 - 1sec, this makes the
game flicker and freeze for a couple of seconds after clicking the hook btn in Deimos. The default is now off there and
unchanged on Windows. A value already saved in settings.json is never touched.

Run from the repo root with the same Python that runs Deimos:
  python poc/wine_client_resizing_poc.py
"""

from src.settings_manager import DEFAULT_SETTINGS, running_under_wine

print(f"running under Wine: {running_under_wine()}")
print(f"client_resizing default: {DEFAULT_SETTINGS['client_resizing']}")
