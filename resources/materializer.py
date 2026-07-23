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
from durable import fsync_directory, replace as durable_replace
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

    def materialize(
        self,
        declaration: SourceDeclaration,
        *,
        operation_id: str | None = None,
    ) -> StagedCandidate:
        if hasattr(declaration.origin, "path"):
            return self._materialize_repo_file(declaration, operation_id=operation_id)
        return self._materialize_https(declaration, operation_id=operation_id)

    def import_file(
        self,
        declaration: SourceDeclaration,
        import_path: Path,
        evidence_path: str,
        *,
        operation_id: str | None = None,
    ) -> StagedCandidate:
        """Privately import one legacy file into a declared source during activation only."""
        try:
            data = import_path.read_bytes()
        except OSError as exc:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.MATERIALIZATION_FAILED,
                        source=declaration.name,
                        detail=f"legacy import {type(exc).__name__}: {exc}",
                    ),
                )
            ) from exc
        if len(data) > self.config.fetch.max_response_bytes:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_RESPONSE_LIMIT_EXCEEDED,
                        url=evidence_path,
                        limit_bytes=self.config.fetch.max_response_bytes,
                    ),
                )
            )
        self._validate_text(data, declaration.mime, declaration.mime, evidence_path)
        return self._stage(
            declaration,
            data,
            observed_mime=declaration.mime,
            origin_evidence={
                "kind": "legacy-import",
                "path": evidence_path,
                "declared_origin": self._declared_origin_evidence(declaration),
            },
            operation_id=operation_id,
        )

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

    def _materialize_repo_file(
        self,
        declaration: SourceDeclaration,
        *,
        operation_id: str | None,
    ) -> StagedCandidate:
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
        path = resolve_repo_file(
            RepoFileOrigin(path_value),
            self.repository_root,
            publishable_paths=self.config.repo_files.publishable_paths,
        )
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
            operation_id=operation_id,
        )

    def _materialize_https(
        self,
        declaration: SourceDeclaration,
        *,
        operation_id: str | None,
    ) -> StagedCandidate:
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
            try:
                parsed = urlsplit(current_url)
                host = parsed.hostname
                port = parsed.port or 443
            except ValueError as exc:
                raise self._redirect_denied(current_url, "invalid URL or port") from exc
            if not host:
                raise self._redirect_denied(current_url, "missing host")
            if parsed.scheme != "https" or host.casefold() != original_host.casefold():
                raise self._redirect_denied(current_url, "redirect must retain https and original host")
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
                    operation_id=operation_id,
                )
            finally:
                response.close()

    def _request(self, url: str, destination: Destination):
        parsed = urlsplit(url)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        host_header = f"[{destination.host}]" if ":" in destination.host else destination.host
        if destination.port != 443:
            host_header = f"{host_header}:{destination.port}"
        try:
            connection = self._connection_factory(
                destination.host,
                destination.port,
                destination.address,
                self.config.fetch.request_timeout_seconds,
                self._context,
            )
            connection.request("GET", target, headers={"Host": host_header, "User-Agent": USER_AGENT})
            response = connection.getresponse()
            response._scout_connection = connection
            return response
        except (OSError, http.client.HTTPException) as exc:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.FETCH_CONNECT_FAILED,
                        host=destination.host,
                        address=destination.address,
                        detail="HTTPS connection failed",
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
            if length < 0:
                raise ScoutDiagnosticsError(
                    (
                        diagnostic(
                            DiagnosticCode.MATERIALIZATION_FAILED,
                            source=url,
                            detail="negative Content-Length",
                        ),
                    )
                )
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
        try:
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
        except ScoutDiagnosticsError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.MATERIALIZATION_FAILED,
                        source=url,
                        detail="response read failed",
                    ),
                )
            ) from exc
        if declared_length is not None and total != length:
            raise ScoutDiagnosticsError(
                (
                    diagnostic(
                        DiagnosticCode.MATERIALIZATION_FAILED,
                        source=url,
                        detail=f"Content-Length mismatch: declared {length}, received {total}",
                    ),
                )
            )
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
        try:
            parsed = urlsplit(candidate)
            parsed.port
        except ValueError as exc:
            raise Materializer._redirect_denied(candidate, "invalid URL or port") from exc
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
        operation_id: str | None,
    ) -> StagedCandidate:
        snapshot_id = uuid.uuid4().hex
        root = artifact_root(self.resources_root, declaration.name)
        if operation_id is None:
            staging_dir = root / "staging" / snapshot_id
        else:
            staging_dir = (
                self.resources_root
                / ".scout-staging"
                / operation_id
                / root.name
                / "generations"
                / snapshot_id
            )
        content_path = staging_dir / "content"
        staging_dir.mkdir(parents=True, exist_ok=False)
        fsync_directory(staging_dir.parent)
        try:
            with content_path.open("wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            fsync_directory(staging_dir)
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

    @staticmethod
    def _declared_origin_evidence(declaration: SourceDeclaration) -> dict[str, str]:
        origin = declaration.origin
        url = getattr(origin, "url", None)
        if isinstance(url, str):
            return {"kind": "https", "url": url}
        path = getattr(origin, "path", None)
        if isinstance(path, str):
            return {"kind": "repo-file", "path": path}
        return {"kind": "unknown"}


def commit_candidate(candidate: StagedCandidate, resources_root: Path) -> Path:
    """Move a validated private candidate into its immutable snapshot generation."""
    destination = resources_root / candidate.snapshot.artifact_path
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
    operation_staging = resources_root / ".scout-staging"
    try:
        operation_relative = candidate.staging_dir.relative_to(operation_staging)
    except ValueError:
        operation_relative = None
    if operation_relative is not None:
        source_root = candidate.staging_dir.parents[1]
        destination_root = destination.parents[2]
        destination_generations = destination.parent.parent
        if not destination_root.exists():
            durable_replace(source_root, destination_root)
        elif not destination_generations.exists():
            durable_replace(source_root / "generations", destination_generations)
        else:
            durable_replace(candidate.staging_dir, destination.parent)
        _remove_empty_ancestors(
            source_root if source_root.exists() else source_root.parent,
            operation_staging,
        )
    else:
        destination.parent.parent.mkdir(parents=True, exist_ok=True)
        fsync_directory(destination.parents[2])
        fsync_directory(destination.parent.parent)
        durable_replace(candidate.staging_dir, destination.parent)
        _remove_empty_ancestors(candidate.staging_dir.parent, candidate.staging_dir.parents[1])
    return destination


def _remove_empty_ancestors(path: Path, stop: Path) -> None:
    current = path
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        fsync_directory(current.parent)
        current = current.parent
