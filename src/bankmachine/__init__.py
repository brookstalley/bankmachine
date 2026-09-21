"""bankmachine -- a read-only, local-first personal finance datastore and MCP server.

It never moves money. The name reads as "ATM" in some markets, so the read-only
framing leads every summary of this package rather than trailing it.

The version is not re-exported here. `pyproject.toml` declares it, the package
metadata carries it, and `build_id.build_identity()` is what reports the running
build -- including the honest `unknown` for a source tree that was never
installed, which a literal in this file could only ever answer with a guess.
"""
