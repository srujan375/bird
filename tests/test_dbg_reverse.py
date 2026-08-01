import re
from bird.harnesses.arch.reverse_seed import _extract_facet, _symbol_name


def test_dbg_api():
    print("SYM get_orders ->", repr(_symbol_name("get_orders")))
    print("SYM post_order ->", repr(_symbol_name("post_order")))
    r = re.compile(r"\b(get|post|put|patch|delete|head|options)\b", re.I)
    print("RE get_orders ->", r.search("get_orders"))
    print("RE post_order ->", r.search("post_order"))
    facet, inf = _extract_facet("api", ["get_orders", "post_order", "helper"], "orders.py")
    print("FACET", facet, "INF", inf)
    assert True