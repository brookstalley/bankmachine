"""The derivation registry, composed where both layers are already in scope.

`store.derivation.DERIVERS` is empty and stays empty, because populating it
would mean `store` importing `connector` -- which would pull the aggregator SDK
into every process that opens the datastore, the read-only query surface
included. That surface must never load the network layer at all, and an import
graph is a better guarantee of that than a rule about who calls what.

So the registry is assembled here instead, one level above both, and passed
explicitly by whoever runs a derivation. It is a plain module-level mapping
rather than anything that registers on import: what a rebuild replays is then a
property of this build, not of which modules happened to be loaded first.

A second aggregator would add one line here and one module beside
`connector/plaid/`, which is exactly the shape §9.2's answer was chosen for.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from bankmachine.connector.plaid.derivers import PLAID_DERIVERS
from bankmachine.store.derivation import Deriver

#: Every endpoint this build can turn into rows, keyed by the aggregator's own
#: path -- the same key `store.raw` records, so a rebuild years from now can
#: still tell what a stored response was.
ALL_DERIVERS: Mapping[str, Deriver] = MappingProxyType(dict(PLAID_DERIVERS))
