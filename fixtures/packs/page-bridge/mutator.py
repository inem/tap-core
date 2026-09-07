"""Controlled HTML hook. The reserved fixture URL has no installed route yet."""
from urllib.parse import urlsplit


def response(flow):
    url = urlsplit(flow.request.pretty_url)
    if (url.scheme, url.netloc) != ("https", "fixture.example") or flow.response is None:
        return
    if "text/html" not in flow.response.headers.get("content-type", "").lower():
        return
    if flow.response.stream:
        return
    html = flow.response.get_text()
    if 'id="tap-fixture"' in html:
        return
    script = '<script type="module" id="tap-fixture" src="/__tap/fixture/page.js"></script>'
    flow.response.set_text(html.replace("</body>", script + "</body>", 1) if "</body>" in html else html + script)
    for name in ("content-length", "etag", "last-modified"):
        flow.response.headers.pop(name, None)
    flow.response.headers["cache-control"] = "no-store"
