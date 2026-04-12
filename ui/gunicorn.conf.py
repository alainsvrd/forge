# gunicorn.conf.py — loads .env before Django settings are imported
import os

env_file = '/opt/forge/ui/.env'
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                os.environ[key] = value

bind = '0.0.0.0:8100'
workers = 1  # Must be 1 — ClaudeCodeManager singleton must live in a single process
timeout = 120
worker_class = 'uvicorn.workers.UvicornWorker'
