from incident_response.runbooks_loader import load_runbooks


def test_loads_runbooks_with_frontmatter(runbooks_dir):
    books = load_runbooks(runbooks_dir)
    slugs = {b.slug for b in books}
    assert "checkout-error-rate" in slugs
    checkout = next(b for b in books if b.slug == "checkout-error-rate")
    assert checkout.title == "Checkout service elevated error rate"
    assert "checkout" in checkout.tags
    assert "error_rate" in checkout.tags
    assert "First actions" in checkout.content


def test_missing_directory_returns_empty(tmp_path):
    assert load_runbooks(tmp_path / "does-not-exist") == []
