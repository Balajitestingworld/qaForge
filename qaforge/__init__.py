"""QA Forge - enterprise Playwright + pytest automation framework.

Layers (top calls down):
    tests -> business flows -> page objects / components -> core engine
Shared services: data (factories, context, ledger), verifiers (events, DB, API),
compliance (masked logs, evidence packs), integrations (JIRA), config & secrets.
"""

__version__ = "0.1.0"
