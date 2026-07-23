"""Source control plane package.

Loaded as a single module object by callers and the scenario suite alike. It
re-exports the full public surface and owns the runtime "seams"
(fetch/index/publish/config functions) that the scenario suite monkeypatches on
this module object; call sites inside the package resolve those seams through
this package at call time, so a patch applied here takes effect — reproducing
the original single-file namespace semantics.
"""
from activation import transition_lock
from config import load_config
from diagnostics import DiagnosticCode, ScoutDiagnosticsError, diagnostic
from materializer import commit_candidate
from source_index import build_generation, open_validated_generation
from source_model import AddRow, SourceRecord, parse_row, snapshot_to_json

from .outcomes import (
    BatchResult,
    RowOutcome,
    _ParsedRow,
    _PreparedPublication,
    _RecoveredOperation,
    _RowExecutionFailure,
)
from .plane import SourceControl

__all__ = [
    "SourceControl",
    "BatchResult",
    "RowOutcome",
    "AddRow",
    "SourceRecord",
    "parse_row",
    "snapshot_to_json",
    "diagnostic",
    "DiagnosticCode",
    "ScoutDiagnosticsError",
    "build_generation",
    "commit_candidate",
    "load_config",
    "open_validated_generation",
    "transition_lock",
]