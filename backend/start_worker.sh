#!/bin/bash
# Start both the Restate handler and the Worker HTTP server
python worker_http.py &
exec python restate_worker.py
