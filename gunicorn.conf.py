# Gunicorn configuration for Arxivist
# Sync workers are intentional: tools.py uses a module-level _session dict
# that is not thread-safe. Each sync worker handles exactly one request at a
# time, keeping sessions isolated across concurrent requests.

bind = "0.0.0.0:5050"
workers = 2
worker_class = "sync"

# Agent requests can take several minutes (LLM + multiple tool calls).
timeout = 300
graceful_timeout = 30
keepalive = 5

# Log to stdout/stderr so Docker captures them via `docker logs`.
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Do not preload — each worker imports the app independently, which is
# required so each worker gets its own _session state in tools.py.
preload_app = False
