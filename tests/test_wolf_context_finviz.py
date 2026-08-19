"""Regression tests for Finviz snapshot-table parsing."""

from core import wolf_context as wc


class _Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None


def test_finviz_nested_cells_do_not_capture_later_share_price(monkeypatch):
    html = """
    <table>
      <tr>
        <td class="snapshot-td2-cp">Short Float</td>
        <td class="snapshot-td2"><a><b>4.21%</b></a></td>
        <td class="snapshot-td2-cp">Short Ratio</td>
        <td class="snapshot-td2"><a><b>0.88</b></a></td>
        <td class="snapshot-td2-cp">Price</td>
        <td class="snapshot-td2">236.08</td>
      </tr>
      <tr>
        <td class="snapshot-td2-cp">Earnings</td>
        <td class="snapshot-td2"><a><b>Aug 27 AMC</b></a></td>
      </tr>
    </table>
    """
    monkeypatch.setattr(wc.requests, "get", lambda *args, **kwargs: _Response(html))
    wc._CACHE.clear()

    result = wc._fetch_finviz("MRVL")

    assert result["short_float"] == 4.21
    assert result["days_to_cover"] == 0.88
    assert result["earnings_date"] == "Aug 27 AMC"


def test_finviz_rejects_implausible_vendor_values(monkeypatch):
    html = """
    <table><tr>
      <td>Short Float</td><td><b>147.00%</b></td>
      <td>Short Ratio</td><td><b>1175.10</b></td>
    </tr></table>
    """
    monkeypatch.setattr(wc.requests, "get", lambda *args, **kwargs: _Response(html))
    wc._CACHE.clear()

    result = wc._fetch_finviz("LLY")

    assert "short_float" not in result
    assert "days_to_cover" not in result
