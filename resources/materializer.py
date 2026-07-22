#!/usr/bin/env python3
"""Pinned remote and repository-local source materialization into private candidates."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from config import ScoutConfig
from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from source_model import (
    HttpsOrigin,
    RepoFileOrigin,
    SourceDeclaration,
    SourceSnapshot,
    artifact_root,
    resolve_repo_file,
)


USER_AGENT = "localmodal-scout/2.0"
_REDIRECT_STATUS = frozenset((301, 302, 303, 307, 308))


@dataclass(frozen=True)
class Destination:
    host: str
    port: int
    address: str


@dataclass(frozen=True)
class StagedCandidate:
    declaration: SourceDeclaration
    snapshot: SourceSnapshot
    staging_dir: Path
    content_path: Path

    def journal_reference(self, resources_root: Path) -> dict[str, object]:
        return {
            "staging_path": self.staging_dir.relative_to(resources_root).as_posix(),
            "snapshot": {
                "snapshot_id": self.snapshot.snapshot_id,
                "sha256": self.snapshot.sha256,
                "byte_count": self.snapshot.byte_count,
            },
        }


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP peer is an already admitted literal address."""

    def __init__(self, host: str, port: int, address: str, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host=host, port=port, timeout=timeout, context=context)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class Materializer:
    """Materialize exactly one declared origin without source discovery."""

    def __init__(
        self,
        resources_root: Path,
        repository_root: Path,
        config: ScoutConfig,
        *,
        resolver: Callable[..., Iterable[tuple]] = socket.getaddrinfo,
        connection_factory: Callable[[str, int, str, float, ssl.SSLContext], object] | None = None,
    ) -> None:
        self.resources_root = resources_root
        self.repository_root = repository_root
        self.config = config
        self._resolver = resolver
        self._context = self._ssl_context()
        self._connection_factory = connection_factory or self._new_connection

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()

    def materialize(self, declaration: SourceDeclaration) -> StagedCandidate:
        if hasattr(declaration.origin, "path"):
            return self._materialize_repo_file(declaration)
        return self._materialize_https(declaration)

    def admit_destination(self, host: str, port: int) -> tuple[Destination, ...]:
        """Resolve once and reject any non-global candidate before connecting."""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                answers = tuple(self._resolver(host, port, type=socket.SOCK_STREAM))
            except OSError as exc:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.DESTINATION_RESOLUTION_FAILED,
                            host=host,
                            detail=f"{type(exc).__name__}: {exc}",
                        ),
                    )
                ) from exc
            addresses = []
            for answer in answers:
                address = answer[4][0]
                if address not in addresses:
                    addresses.append(address)
            if not addresses:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.DESTINATION_RESOLUTION_FAILED,
                            host=host,
                            detail="no stream addresses returned",
                        ),
                    )
                )
        else:
            addresses = [str(literal)]

        admitted: list[Destination] = []
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.DESTINATION_DENIED,
                            host=host,
                            address=str(parsed),
                            reason="address is not globally routable",
                        ),
                    )
                )
            admitted.append(Destination(host=host, port=port, address=str(parsed)))
        return tuple(admitted)

    def _materialize_repo_file(self, declaration: SourceDeclaration) -> StagedCandidate:
        origin = declaration.origin
        path_value = getattr(origin, "path", None)
        if not isinstance(path_value, str):
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.MATERIALIZATION_FAILED,
                        source=declaration.name,
                        detail="repo-file origin is missing path",
                    ),
                )
            )
        path = resolve_repo_file(RepoFileOrigin(path_value), self.repository_root)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.MATERIALIZATION_FAILED,
                        source=declaration.name,
                        detail=f"{type(exc).__name__}: {exc}",
                    ),
                )
            ) from exc
        if len(data) > self.config.fetch.max_response_bytes:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_RESPONSE_LIMIT_EXCEEDED,
                        url=origin.path,
                        limit_bytes=self.config.fetch.max_response_bytes,
                    ),
                )
            )
        self._validate_text(data, declaration.mime, declaration.mime, origin.path)
        return self._stage(
            declaration,
            data,
            observed_mime=declaration.mime,
            origin_evidence={"kind": "repo-file", "path": origin.path},
        )

    def _materialize_https(self, declaration: SourceDeclaration) -> StagedCandidate:
        origin = declaration.origin
        url_value = getattr(origin, "url", None)
        if not isinstance(url_value, str):
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.MATERIALIZATION_FAILED,
                        source=declaration.name,
                        detail="https origin is missing url",
                    ),
                )
            )
        current_url = url_value
        original_host = self._url_host(current_url)
        redirects = 0
        while True:
            parsed = urlsplit(current_url)
            host = parsed.hostname
            if not host:
                raise self._redirect_denied(current_url, "missing host")
            if parsed.scheme != "https" or host.casefold() != original_host.casefold():
                raise self._redirect_denied(current_url, "redirect must retain https and original host")
            port = parsed.port or 443
            destination = self.admit_destination(host, port)[0]
            response = self._request(current_url, destination)
            try:
                if response.status in _REDIRECT_STATUS:
                    location = response.getheader("Location")
                    if not location:
                        raise self._redirect_denied(current_url, "redirect response omitted Location")
                    if redirects >= self.config.fetch.max_redirects:
                        raise self._redirect_denied(current_url, "redirect limit exceeded")
                    redirects += 1
                    current_url = self._normal_redirect(current_url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise ScoutDiagnosticsError(
                        (
                            diagnostic(
                                DiagnosticCode.MATERIALIZATION_FAILED,
                                source=declaration.name,
                                detail=f"HTTPS status {response.status}",
                            ),
                        )
                    )
                data, observed_mime = self._read_response(response, current_url)
                self._validate_text(data, declaration.mime, observed_mime, current_url)
                return self._stage(
                    declaration,
                    data,
                    observed_mime=observed_mime,
                    origin_evidence={
                        "kind": "https",
                        "initial_url": origin.url,
                        "final_url": current_url,
                        "host": host,
                        "address": destination.address,
                    },
                )
            finally:
                response.close()

    def _request(self, url: str, destination: Destination):
        parsed = urlsplit(url)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection = self._connection_factory(
                destination.host,
                destination.port,
                destination.address,
                self.config.fetch.request_timeout_seconds,
                self._context,
            )
            connection.request("GET", target, headers={"Host": destination.host, "User-Agent": USER_AGENT})
            response = connection.getresponse()
            response._scout_connection = connection
            return response
        except OSError as exc:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_CONNECT_FAILED,
                        host=destination.host,
                        address=destination.address,
                        detail=f"{type(exc).__name__}: {exc}",
                    ),
                )
            ) from exc

    def _new_connection(
        self, host: str, port: int, address: str, timeout: float, context: ssl.SSLContext
    ) -> _PinnedHTTPSConnection:
        return _PinnedHTTPSConnection(host, port, address, timeout, context)

    def _read_response(self, response, url: str) -> tuple[bytes, str]:
        declared_length = response.getheader("Content-Length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.MATERIALIZATION_FAILED,
                            source=url,
                            detail="invalid Content-Length",
                        ),
                    )
                ) from exc
            if length > self.config.fetch.max_response_bytes:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.FETCH_RESPONSE_LIMIT_EXCEEDED,
                            url=url,
                            limit_bytes=self.config.fetch.max_response_bytes,
                        ),
                    )
                )
        chunks = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, self.config.fetch.max_response_bytes + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > self.config.fetch.max_response_bytes:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.FETCH_RESPONSE_LIMIT_EXCEEDED,
                            url=url,
                            limit_bytes=self.config.fetch.max_response_bytes,
                        ),
                    )
                )
            chunks.append(chunk)
        return b"".join(chunks), self._content_type(response, url)

    @staticmethod
    def _content_type(response, url: str) -> str:
        raw = response.getheader("Content-Type")
        if not raw:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_MIME_MISMATCH,
                        expected="declared MIME",
                        observed="absent",
                    ),
                )
            )
        message = Message()
        message["Content-Type"] = raw
        charset = message.get_content_charset()
        if charset is not None and charset.casefold() != "utf-8":
            raise ScoutDiagnosticsError(
                (diagnostic(DiagnosticCode.FETCH_CHARSET_INVALID, charset=charset),)
            )
        return message.get_content_type().casefold()

    @staticmethod
    def _validate_text(data: bytes, expected_mime: str, observed_mime: str, url: str) -> None:
        if observed_mime != expected_mime:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_MIME_MISMATCH,
                        expected=expected_mime,
                        observed=observed_mime,
                    ),
                )
            )
        try:
            data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_UTF8_INVALID,
                        url=url,
                        detail=f"byte {exc.start}",
                    ),
                )
            ) from exc

    @staticmethod
    def _url_host(url: str) -> str:
        host = urlsplit(url).hostname
        if not host:
            raise ValueError(f"URL has no host: {url!r}")
        return host

    @staticmethod
    def _normal_redirect(current_url: str, location: str) -> str:
        candidate = urljoin(current_url, location)
        parsed = urlsplit(candidate)
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_REDIRECT_DENIED,
                        url=candidate,
                        reason="credentials and fragments are forbidden",
                    ),
                )
            )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))

    @staticmethod
    def _redirect_denied(url: str, reason: str) -> ScoutDiagnosticsError:
        return ScoutDiagnosticsError(
            (diagnostic(DiagnosticCode.FETCH_REDIRECT_DENIED, url=url, reason=reason),)
        )

    def _stage(
        self,
        declaration: SourceDeclaration,
        data: bytes,
        *,
        observed_mime: str,
        origin_evidence: dict[str, object],
    ) -> StagedCandidate:
        snapshot_id = uuid.uuid4().hex
        root = artifact_root(self.resources_root, declaration.name)
        staging_dir = root / "staging" / snapshot_id
        content_path = staging_dir / "content"
        staging_dir.mkdir(parents=True, exist_ok=False)
        try:
            content_path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            snapshot = SourceSnapshot(
                snapshot_id=snapshot_id,
                materialized_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                artifact_path=(root.name + f"/generations/{snapshot_id}/content"),
                sha256=digest,
                byte_count=len(data),
                observed_mime=observed_mime,
                origin_evidence=origin_evidence,
            )
            return StagedCandidate(
                declaration=declaration,
                snapshot=snapshot,
                staging_dir=staging_dir,
                content_path=content_path,
            )
        except Exception:
            if staging_dir.exists():
                for item in staging_dir.iterdir():
                    item.unlink(missing_ok=True)
                staging_dir.rmdir()
            raise


def commit_candidate(candidate: StagedCandidate, resources_root: Path) -> Path:
    """Move a validated private candidate into its immutable snapshot generation."""
    destination = resources_root / candidate.snapshot.artifact_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ScoutDiagnosticsError(
            (
                diagnostic(
                    DiagnosticCode.MATERIALIZATION_FAILED,
                    source=candidate.declaration.name,
                    detail="snapshot artifact already exists",
                ),
            )
        )
    os.replace(candidate.content_path, destination)
    candidate.staging_dir.rmdir()
    staging_parent = candidate.staging_dir.parent
    if not any(staging_parent.iterdir()):
        staging_parent.rmdir()
    return destination
