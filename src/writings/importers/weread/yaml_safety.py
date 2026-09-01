"""Bounded YAML loader shared by private WeRead metadata and plans."""

from __future__ import annotations

import yaml


MAX_YAML_DEPTH = 64
MAX_YAML_NODES = 500_000


class BoundedSafeLoader(yaml.SafeLoader):
    """Stop composition before recursive YAML can exhaust Python resources."""

    def __init__(self, stream) -> None:
        super().__init__(stream)
        self._bounded_depth = 0
        self._bounded_nodes = 0

    def compose_node(self, parent, index):
        self._bounded_depth += 1
        self._bounded_nodes += 1
        try:
            if (
                self._bounded_depth > MAX_YAML_DEPTH
                or self._bounded_nodes > MAX_YAML_NODES
            ):
                raise yaml.YAMLError("YAML resource limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._bounded_depth -= 1


__all__ = ["BoundedSafeLoader", "MAX_YAML_DEPTH", "MAX_YAML_NODES"]
