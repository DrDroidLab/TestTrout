"""Domain types shared by every layer.

These models are the project's public vocabulary. They are pure data: no I/O,
no model calls, and no knowledge of any particular framework. Adapters convert
framework-specific reality *into* these types; everything downstream consumes
only these types.

Changing a field here is a breaking change for adapter authors. Treat this
package as public API and version it accordingly.
"""

from testtrout.domain.config import (
    Config,
    Entrypoint,
    EntrypointKind,
    ExternalRule,
    IsolationStrategy,
    ModelConfig,
    SubstitutionConfig,
    TestUser,
)
from testtrout.domain.surface import (
    Column,
    Criticality,
    DataOperation,
    EdgeFunction,
    Endpoint,
    ExternalDependency,
    Operation,
    Policy,
    ScanResult,
    Screen,
    ServerAction,
    SourceLocation,
    Surface,
    SurfaceKind,
    Table,
)

__all__ = [
    "Column",
    "Config",
    "Criticality",
    "DataOperation",
    "EdgeFunction",
    "Endpoint",
    "Entrypoint",
    "EntrypointKind",
    "ExternalDependency",
    "ExternalRule",
    "IsolationStrategy",
    "ModelConfig",
    "Operation",
    "Policy",
    "ScanResult",
    "Screen",
    "ServerAction",
    "SourceLocation",
    "SubstitutionConfig",
    "Surface",
    "SurfaceKind",
    "Table",
    "TestUser",
]
