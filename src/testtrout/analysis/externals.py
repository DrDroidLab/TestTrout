"""Third-party dependency detection — the substitution boundary.

A dependency is listed here when calling it for real during a test would cost
money, send something to a human, or produce a nondeterministic result. Those
are exactly the calls that must be intercepted, so this table doubles as the
default contract set for the substitution proxy.

Adding a vendor is a one-line change and a welcome contribution.
"""

from __future__ import annotations

from dataclasses import dataclass

from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.parser import SourceFile, find_all
from testtrout.domain.surface import ExternalDependency, SourceLocation


@dataclass(frozen=True)
class Vendor:
    """A known third-party service and how to recognise and intercept it."""

    name: str
    packages: tuple[str, ...]
    hosts: tuple[str, ...]
    side_effecting: bool
    """True when a real call has consequences: a charge, an email, a spend."""


VENDORS: tuple[Vendor, ...] = (
    Vendor(
        "stripe",
        ("stripe", "@stripe/stripe-js", "@stripe/react-stripe-js"),
        ("api.stripe.com",),
        True,
    ),
    Vendor("resend", ("resend",), ("api.resend.com",), True),
    Vendor("sendgrid", ("@sendgrid/mail",), ("api.sendgrid.com",), True),
    Vendor("twilio", ("twilio",), ("api.twilio.com",), True),
    Vendor("openai", ("openai",), ("api.openai.com",), True),
    Vendor("anthropic", ("@anthropic-ai/sdk",), ("api.anthropic.com",), True),
    Vendor(
        "posthog", ("posthog-js", "posthog-node"), ("app.posthog.com", "us.i.posthog.com"), False
    ),
    Vendor("segment", ("@segment/analytics-next",), ("api.segment.io",), False),
    Vendor("sentry", ("@sentry/react", "@sentry/nextjs"), ("sentry.io",), False),
    Vendor("mapbox", ("mapbox-gl",), ("api.mapbox.com",), False),
    Vendor("algolia", ("algoliasearch",), ("algolia.net",), False),
)

_BY_PACKAGE = {package: vendor for vendor in VENDORS for package in vendor.packages}


def extract(file: SourceFile, allocator: IdAllocator, seen: set[str]) -> list[ExternalDependency]:
    """Find third-party SDK imports in one file.

    ``seen`` deduplicates across files: one entry per vendor is what the
    substitution proxy needs, not one per import site.
    """
    found: list[ExternalDependency] = []
    for node in find_all(file.root, "import_statement"):
        source = file.text(node.child_by_field_name("source")).strip("\"'")
        # Match the package root so that '@sentry/react/foo' still resolves.
        vendor = _BY_PACKAGE.get(source) or _BY_PACKAGE.get("/".join(source.split("/")[:2]))
        if vendor is None or vendor.name in seen:
            continue
        seen.add(vendor.name)
        found.append(
            ExternalDependency(
                id=allocator.allocate(f"external:{vendor.name}"),
                location=SourceLocation(file=file.rel, line=file.line_of(node)),
                vendor=vendor.name,
                package=source,
                hosts=list(vendor.hosts),
                side_effecting=vendor.side_effecting,
            )
        )
    return found
