"""Reduced core has no declared desktop/F9 configuration panel.

CLI setup is implemented by the provider's post_setup hook, not this schema.
Keep this path-loadable module cold: do not import the provider or host runtime.
"""

CONFIG_SCHEMA = None
