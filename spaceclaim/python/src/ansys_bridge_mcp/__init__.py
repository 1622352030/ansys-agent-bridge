"""Ansys Agent Bridge — an MCP server for Ansys SpaceClaim, built on PyAnsys.

The package deliberately has no import-time dependency on ANSYS. It installs,
imports and answers `ansys-bridge-doctor` on a machine that has never seen an
ANSYS installer; only the SpaceClaim tools need the runtime.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
