"""Synthetics test package.

The suite must behave the same wherever it is launched. An employee session exports
SYNTHETICS_SESSION_RUN and SYNTHETICS_HOME (brain.py), and any process under a launchd job
(a worker, or a suite started from one) inherits XPC_SERVICE_NAME. The product reads all three:
the first two make a CLI call act as that session (authority.actor, owner gates), the third is the
label a runtime restart kickstarts (cli.py serve). Clear them once, when the package is imported,
so no test acts as a real session or names a real launchd job. Tests that need one set it themselves.
"""
import os

SESSION_VARIABLES = ("SYNTHETICS_SESSION_RUN", "SYNTHETICS_HOME", "XPC_SERVICE_NAME")

for _name in SESSION_VARIABLES:
    os.environ.pop(_name, None)
