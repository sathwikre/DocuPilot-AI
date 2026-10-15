#!/usr/bin/env sh
# Launch DocuPilot AI (Linux/macOS). Run setup first: see README.md
cd "$(dirname "$0")"
.venv/bin/python -m streamlit run app.py
