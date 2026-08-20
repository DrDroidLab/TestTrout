"""End-to-end scan behaviour against the fixture application."""

from __future__ import annotations


def test_detects_the_stack(scanned):
    assert scanned.project.framework == "vite-react"
    assert scanned.project.backend == "supabase"
    assert scanned.project.auth == "supabase"
    assert scanned.project.detected_from, "detection must record its evidence"


def test_finds_every_declared_route(scanned):
    assert {s.path for s in scanned.screens} == {
        "/login",
        "/orders",
        "/orders/:id",
        "/checkout",
        "/settings",
    }


def test_extracts_route_parameters(scanned):
    detail = next(s for s in scanned.screens if s.path == "/orders/:id")
    assert detail.params == ["id"]


def test_reachability_crosses_component_boundaries(scanned):
    """The delete lives in OrderActions, two imports below the route component.

    If this breaks, screens stop inheriting the risk of what they can trigger,
    and the ranking that drives test proposals becomes meaningless.
    """
    detail = next(s for s in scanned.screens if s.path == "/orders/:id")
    reached = {op.id for op in scanned.data_operations if op.id in detail.reaches}
    assert "data:orders.delete" in reached
    assert "rpc:recalculate_order_total" in reached


def test_screen_inherits_criticality_from_what_it_reaches(scanned):
    detail = next(s for s in scanned.screens if s.path == "/orders/:id")
    assert detail.criticality.value == "critical"
    assert detail.criticality_reasons


def test_surface_ids_are_stable_across_scans(lovable_shop):
    from testtrout.analysis.scanner import scan

    first = {s.id for s in scan(lovable_shop).all_surfaces()}
    second = {s.id for s in scan(lovable_shop).all_surfaces()}
    assert first == second


def test_policies_and_schema_are_parsed(scanned):
    assert {p.table for p in scanned.policies} == {"orders", "profiles", "payments"}
    assert {t.name for t in scanned.tables} == {"orders", "profiles", "payments", "audit_log"}


def test_third_party_dependency_is_detected(scanned):
    assert any(e.vendor == "stripe" for e in scanned.externals)
