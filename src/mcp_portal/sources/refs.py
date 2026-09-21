"""Resolve `$ref` pointers within an OpenAPI document.

Internal refs (`#/...`) are always resolved. External refs name a URL or file
the *document* controls, so honoring them unconditionally lets the document
dictate which URLs sidekit fetches or which files it reads — an SSRF and
path-traversal vector. They are refused unless the operator opts in with an
explicit host allowlist.
"""

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit


class RefError(Exception):
    """Raised for a `$ref` this document is not permitted to use, or a broken pointer."""


def _split(ref: str) -> tuple[str, str]:
    """Split a `$ref` into its (possibly empty) external target and JSON pointer."""
    target, _, pointer = ref.partition("#")
    return target, ("#" + pointer) if pointer else ""


def _pointer_lookup(root: Mapping[str, Any], pointer: str) -> Any:
    if pointer in ("", "#"):
        return root
    if not pointer.startswith("#/"):
        raise RefError(f"unsupported $ref pointer form: {pointer!r}")
    node: Any = root
    for raw in pointer[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        try:
            node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise RefError(f"$ref pointer {pointer!r} does not resolve: {exc}") from exc
    return node


def _is_networked(target: str) -> bool:
    return bool(urlsplit(target).scheme)


class RefResolver:
    """Resolves every `$ref` in `document`, returning a ref-free copy."""

    def __init__(
        self,
        document: Mapping[str, Any],
        *,
        allow_external: bool,
        allowed_hosts: frozenset[str] = frozenset(),
        fetch_external: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._root = document
        self._allow_external = allow_external
        self._allowed_hosts = allowed_hosts
        self._fetch_external = fetch_external
        self._external_cache: dict[str, Mapping[str, Any]] = {}
        self._resolved_cache: dict[tuple[int, str], Any] = {}
        self.warnings: list[str] = []

    def resolve(self) -> dict[str, Any]:
        result = self._walk(self._root, chain=(), root=self._root)
        assert isinstance(result, dict)
        return result

    def _walk(self, node: Any, chain: tuple[tuple[int, str], ...], root: Mapping[str, Any]) -> Any:
        if isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
            return self._resolve_ref(node["$ref"], chain, root)
        if isinstance(node, Mapping):
            return {k: self._walk(v, chain, root) for k, v in node.items()}
        if isinstance(node, list):
            return [self._walk(v, chain, root) for v in node]
        return node

    def _resolve_ref(
        self, ref: str, chain: tuple[tuple[int, str], ...], root: Mapping[str, Any]
    ) -> Any:
        # Keyed on (which document, ref text): identical ref text can denote
        # different targets in different documents once external refs cross
        # into a shared file, so the ref string alone is not a safe cycle key.
        key = (id(root), ref)
        if key in chain:
            self.warnings.append(f"circular $ref {ref!r} replaced with an open object schema")
            return {"type": "object"}
        # Memoized *outside* the chain: a shared schema referenced from many
        # places (fan-out, not a cycle) would otherwise be re-walked from
        # scratch at every occurrence, which is exponential in the depth of a
        # diamond-shaped reference graph — a third-party document doing this
        # is indistinguishable from an accident and a deliberate DoS.
        if key in self._resolved_cache:
            return self._resolved_cache[key]

        target, pointer = _split(ref)
        # A bare `#/...` ref found while walking an externally-fetched document
        # must resolve against *that* document, not the top-level one — shared
        # component files conventionally cross-reference each other this way.
        new_root = self._external_document(target) if target else root
        node = _pointer_lookup(new_root, pointer or "#")
        result = self._walk(node, chain + (key,), new_root)
        self._resolved_cache[key] = result
        return result

    def _external_document(self, target: str) -> Mapping[str, Any]:
        if not self._allow_external:
            raise RefError(
                f"external $ref target {target!r} is disabled by default: the document "
                "would otherwise dictate which URLs or files sidekit reads. Set "
                "introspection.openapi.allow_external_refs and .allowed_hosts to permit it"
            )
        if _is_networked(target):
            host = urlsplit(target).netloc
            if host not in self._allowed_hosts:
                raise RefError(
                    f"external $ref target {target!r} has host {host!r}, which is not in "
                    "introspection.openapi.allowed_hosts"
                )
        if target not in self._external_cache:
            if self._fetch_external is None:
                raise RefError(
                    f"external $ref target {target!r} cannot be fetched: no fetcher configured"
                )
            self._external_cache[target] = self._fetch_external(target)
        return self._external_cache[target]
