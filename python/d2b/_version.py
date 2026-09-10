"""The one place the version is written.

``pyproject.toml`` reads it (hatch dynamic version), and the client tags
(``X-D2B-Client`` / ``User-Agent``) derive from it, so a release bumps one
line and every surface follows.
"""
__version__ = "0.1.0"
