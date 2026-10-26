from scripts.dashboard_server import REFRESH_META, render_live_page


def test_live_dashboard_refreshes_and_keeps_paper_safety_labels():
    page = render_live_page()
    assert REFRESH_META in page
    assert "PaperTrade Agent" in page
    assert "NO REAL ORDERS" in page
    assert "https://" not in page
    assert "__STATE_DATA__" not in page
    assert "__GENERATED_AT__" not in page
