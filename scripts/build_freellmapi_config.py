"""Build freellmapi's declarative startup config from whichever free provider keys are set.

Reads GROQ_API_KEY, GEMINI_API_KEY, CEREBRAS_API_KEY, MISTRAL_API_KEY, OPENROUTER_API_KEY,
NVIDIA_API_KEY, COHERE_API_KEY from the environment. Add more and the router fails over
across all of them when one hits its free-tier limit.
"""
import json
import os
import sys

PROVIDERS = {
    "groq": "GROQ_API_KEY",
    "google": "GEMINI_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "cohere": "COHERE_API_KEY",
}

email, password = sys.argv[1], sys.argv[2]
keys = [{"platform": p, "key": os.environ[v], "label": "bot"}
        for p, v in PROVIDERS.items() if os.environ.get(v)]
if not keys:
    sys.stderr.write("warning: no LLM provider keys set; bot will run without the LLM check\n")
print(json.dumps({"admin": {"email": email, "password": password},
                  "keys": keys,
                  "routing": {"strategy": "balanced"}}))
