"""Optional local LLM (Ollama) integration.

Used for exactly one task where rules can run out: proposing a semantic role for table headers that
the deterministic dictionary could not map (e.g. 'Amt Recd' -> credit). Suggestions are constrained to
a fixed list of roles, never override a deterministic mapping, never change cell values, and are
recorded in the audit trail. All numerical work stays in Python.

Only header *names* are sent, and only to a local Ollama server. Non-local hosts are refused unless
DOCUPILOT_ALLOW_REMOTE_LLM=1 is set explicitly.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

from config.settings import SETTINGS
from utils.logging_config import get_logger, log_event

log = get_logger("llm")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Roles the LLM may propose (the ones that unlock validation checks), with plain-language hints.
ROLE_HINTS = {
    "description": "text describing the transaction or item",
    "reference": "reference / cheque / transaction number",
    "debit": "money going OUT of the account (paid out, withdrawn, spent)",
    "credit": "money coming IN to the account (received, deposited)",
    "amount": "a single signed transaction amount",
    "quantity": "number of units",
    "unit_price": "price per unit",
    "net_amount": "line amount before tax",
    "tax_rate": "tax percentage",
    "tax_amount": "tax money amount",
    "line_total": "total amount of the line/row",
    "transaction_date": "date of the transaction",
    "paid": "amount already paid",
    "outstanding": "amount still due",
}


@dataclass
class LLMStatus:
    available: bool
    host: str
    models: list[str] = field(default_factory=list)
    selected: Optional[str] = None
    error: Optional[str] = None


class LocalLLM:
    """Thin client for a local Ollama server."""

    def __init__(self, host: Optional[str] = None, model: Optional[str] = None, timeout: Optional[int] = None):
        self.host = (host or SETTINGS.ollama_host).rstrip("/")
        self.model = model
        self.timeout = timeout or SETTINGS.llm_timeout_s
        self.calls = 0
        self.notes: list[str] = []
        parsed = urlparse(self.host)
        if parsed.hostname not in _LOCAL_HOSTS and not SETTINGS.allow_remote_llm:
            raise ValueError(f"Refusing non-local LLM host '{parsed.hostname}'. Documents are processed locally only.")

    def list_models(self) -> list[str]:
        r = requests.get(f"{self.host}/api/tags", timeout=2.5)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]

    def status(self) -> LLMStatus:
        try:
            models = self.list_models()
        except Exception as exc:
            return LLMStatus(False, self.host, error=f"Ollama not reachable ({type(exc).__name__})")
        selected = self.model if self.model in models else self.choose_model(models)
        if not selected:
            return LLMStatus(False, self.host, models, None, "No suitable local model installed")
        return LLMStatus(True, self.host, models, selected)

    @staticmethod
    def choose_model(models: list[str]) -> Optional[str]:
        names = set(models)
        for pref in SETTINGS.preferred_llm_models:
            if pref in names:
                return pref
            for m in models:
                if m.split(":")[0] == pref.split(":")[0] and pref.endswith(":latest"):
                    return m
        return None

    def generate_json(self, prompt: str, schema: Optional[dict] = None) -> Optional[dict]:
        """Run a deterministic (temperature 0) JSON completion, optionally constrained by a JSON schema.

        Returns None on any failure."""
        if not self.model:
            st = self.status()
            if not st.available:
                return None
            self.model = st.selected
        t0 = time.time()
        try:
            r = requests.post(f"{self.host}/api/generate", timeout=self.timeout, json={
                "model": self.model, "prompt": prompt, "format": schema or "json", "stream": False,
                "options": {"temperature": 0, "num_predict": 300},
            })
            r.raise_for_status()
            self.calls += 1
            text = r.json().get("response", "")
            data = json.loads(text)
            log_event(log, "llm call", model=self.model, seconds=round(time.time() - t0, 2))
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("LLM call failed: %s", type(exc).__name__)
            self.notes.append(f"LLM call failed ({type(exc).__name__}); deterministic result kept")
            return None

    def map_headers(self, headers: list[str], allowed_roles: list[str]) -> dict[str, str]:
        """Suggest a role for each header from allowed_roles (or 'none'). Invalid answers are dropped."""
        if not headers:
            return {}
        roles = [r for r in allowed_roles if r in ROLE_HINTS]
        if not roles:
            return {}
        role_lines = "\n".join(f"- {r}: {ROLE_HINTS[r]}" for r in roles)
        prompt = (
            "You label column headers of tables from business documents (invoices, bank statements, reports).\n"
            f"Roles:\n{role_lines}\n- none: none of the above, or unsure\n"
            "Rules: debit and credit are different things; a percentage rate is not an amount; "
            "abbreviations are common (Amt = amount, Recd = received, Bal = balance, Dt = date).\n"
            'Example: {"Withdrawals": "debit", "Deposits": "credit", "Particulars": "description"}\n'
            f"Headers: {json.dumps(headers)}\n"
            'Answer with JSON only, one entry per header: {"<header>": "<role>"}'
        )
        # Constrained decoding: exactly one answer per header, drawn from the allowed labels.
        schema = {"type": "object", "required": list(headers),
                  "properties": {h: {"type": "string", "enum": roles + ["none"]} for h in headers}}
        data = self.generate_json(prompt, schema) or {}
        out = {}
        for h in headers:
            v = data.get(h)
            if isinstance(v, str) and v in allowed_roles:
                out[h] = v
        if out:
            self.notes.append(f"LLM ({self.model}) suggested roles for {len(out)} of {len(headers)} unmapped headers")
        return out


def get_llm_status() -> LLMStatus:
    """Status for the UI, never raises."""
    try:
        return LocalLLM().status()
    except Exception as exc:
        return LLMStatus(False, SETTINGS.ollama_host, error=str(exc))
