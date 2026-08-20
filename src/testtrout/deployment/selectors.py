"""Extracting stable ways to address elements on a probed screen.

This is groundwork for Phase 4. A generated browser test is only as durable as
its selectors, and the single most common reason such suites get abandoned is
that they were written against generated class names and broke on the next
restyle.

So candidates are collected in order of durability — test id, accessible role
and name, label, visible text — and a screen with nothing better than CSS is
flagged, because that is a signal to propose adding ``data-testid`` attributes
rather than to generate brittle tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from testtrout.domain.observation import SelectorCandidate, SelectorStrategy

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

# Enough to characterise a screen without turning the observation file into a
# DOM dump. Phase 4 needs representative anchors, not exhaustive ones.
MAX_CANDIDATES = 40

# Runs in the page. Kept to plain DOM APIs so it works on any framework, and
# deliberately read-only — it must never trigger application behaviour.
_EXTRACT_JS = """
() => {
  const out = [];
  const seen = new Set();
  const text = (el) => (el.innerText || el.textContent || '').trim().slice(0, 80);
  const push = (item) => {
    const key = item.strategy + '|' + item.value + '|' + (item.name || '');
    if (!seen.has(key)) { seen.add(key); out.push(item); }
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  for (const el of document.querySelectorAll('[data-testid],[data-test-id],[data-test]')) {
    const id = el.getAttribute('data-testid') || el.getAttribute('data-test-id')
             || el.getAttribute('data-test');
    if (id) push({ strategy: 'test_id', value: id, description: text(el) });
  }

  const roleOf = (el) => {
    if (el.hasAttribute('role')) return el.getAttribute('role');
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button') return 'button';
      return 'textbox';
    }
    return null;
  };

  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref) return text(ref);
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return text(label);
    }
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim();
    return text(el);
  };

  for (const el of document.querySelectorAll(
    'button, a[href], input, select, textarea, h1, h2, h3, [role]'
  )) {
    if (!visible(el)) continue;
    const role = roleOf(el);
    if (!role) continue;
    const name = nameOf(el);
    if (!name) continue;
    push({ strategy: 'role', value: role, role, name, description: text(el) });
  }

  for (const el of document.querySelectorAll('label')) {
    const t = text(el);
    if (t && visible(el)) push({ strategy: 'label', value: t, description: '' });
  }

  return out;
}
"""


def extract(page: Page) -> list[SelectorCandidate]:
    """Collect selector candidates from the current page, most stable first.

    Returns an empty list rather than raising if extraction fails — a screen
    whose DOM could not be read is still a screen worth reporting as reachable.
    """
    try:
        raw: list[dict[str, Any]] = page.evaluate(_EXTRACT_JS)
    except Exception:
        return []

    candidates: list[SelectorCandidate] = []
    for item in raw:
        try:
            strategy = SelectorStrategy(item["strategy"])
        except (KeyError, ValueError):
            continue
        candidates.append(
            SelectorCandidate(
                strategy=strategy,
                value=str(item.get("value", "")),
                role=item.get("role"),
                name=item.get("name"),
                description=str(item.get("description", ""))[:80],
            )
        )

    candidates.sort(key=lambda c: (c.strategy.rank, c.value))
    return candidates[:MAX_CANDIDATES]


def best_strategy(candidates: list[SelectorCandidate]) -> SelectorStrategy | None:
    """The most durable strategy available on a screen.

    Used to flag screens where generated tests would have nothing stable to
    hold onto.
    """
    return min((c.strategy for c in candidates), key=lambda s: s.rank, default=None)
