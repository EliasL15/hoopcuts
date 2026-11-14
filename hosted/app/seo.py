"""Public HTML metadata and crawl controls, independent of request Host headers."""
import json
import re
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from fastapi.responses import HTMLResponse, Response

from .config import settings


def origin():
    url = urlsplit(settings.app_base_url)
    return f"{url.scheme}://{url.netloc}"


def pages(frontend: Path):
    return json.loads((frontend / "seo.json").read_text())


def html(frontend: Path, path: str, status_code=200):
    public = pages(frontend).get(path)
    prerender = frontend / "prerender" / ("home.html" if path == "/" else path.strip("/") + ".html")
    # Only known public routes can select a pre-rendered file.
    source = prerender if public and prerender.is_file() else frontend / "index.html"
    document = source.read_text()
    base = origin()
    title = public["title"] if public else "Your private film room | HoopCuts"
    description = public["description"] if public else "Sign in to manage your private HoopCuts basketball game library and account."
    robots = "index, follow, max-image-preview:large" if public else "noindex, follow"
    document = re.sub(r"<title>.*?</title>", f"<title>{escape(title)}</title>", document)
    document = re.sub(r'<meta (?:name|property)="(?:description|robots|site-origin|og:title|og:description|og:url|twitter:title|twitter:description)"[^>]*>', "", document)
    document = re.sub(r'<link rel="canonical"[^>]*>', "", document)
    # Replace legacy deployment origins in the image metadata too.
    document = document.replace('content="/assets/brand/hoopcuts-symbol.svg"', f'content="{escape(base, quote=True)}/assets/brand/hoopcuts-symbol.svg"')
    head = f'<meta name="site-origin" content="{escape(base, quote=True)}"><meta name="robots" content="{robots}">'
    for attribute, key, value in [("name", "description", description), ("property", "og:title", title), ("property", "og:description", description), ("property", "og:url", base + path), ("name", "twitter:title", title), ("name", "twitter:description", description)]:
        head += f'<meta {attribute}="{key}" content="{escape(value, quote=True)}">'
    if public:
        head += f'<link rel="canonical" href="{escape(base + path, quote=True)}">'
        graph = [{"@type": "WebSite", "@id": base + "/#website", "url": base + "/", "name": "HoopCuts"},
                 {"@type": "WebPage", "url": base + path, "name": title, "description": description, "isPartOf": {"@id": base + "/#website"}}]
        if path == "/":
            graph.append({"@type": "WebApplication", "name": "HoopCuts", "url": base + "/", "applicationCategory": "MultimediaApplication", "operatingSystem": "Web browser", "description": description, "featureList": ["Basketball game highlight detection", "Clip review and selection", "Vertical and landscape highlight reels", "Optional sponsor end cards"]})
        payload = json.dumps({"@context": "https://schema.org", "@graph": graph}).replace("<", "\\u003c")
        head += f'<script id="seo-schema" data-path="{path}" type="application/ld+json">{payload}</script>'
    return HTMLResponse(document.replace("</head>", head + "</head>"), status_code=status_code, headers={"X-Robots-Tag": robots})


def sitemap(frontend):
    entries = "".join(f"<url><loc>{xml_escape(origin() + path)}</loc></url>" for path in pages(frontend))
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>', media_type="application/xml")


def robots():
    return Response(f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /internal/\nSitemap: {origin()}/sitemap.xml\n", media_type="text/plain")
