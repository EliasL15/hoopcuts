"""Crawl controls must apply before JavaScript and follow the configured domain."""
import json
import re
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from hosted.app import main, seo


@pytest.fixture
def site(tmp_path, monkeypatch):
    frontend = Path(__file__).resolve().parents[1] / "frontend"
    (tmp_path / "index.html").write_text((frontend / "index.html").read_text())
    (tmp_path / "seo.json").write_text((frontend / "public/seo.json").read_text())
    (tmp_path / "prerender").mkdir()
    for path in seo.pages(tmp_path):
        filename = "home" if path == "/" else path[1:]
        template = (tmp_path / "index.html").read_text()
        (tmp_path / "prerender" / f"{filename}.html").write_text(template.replace('<div id="root"></div>', '<div id="root"><h1>Public content</h1></div>'))
    monkeypatch.setattr(main, "FRONTEND", tmp_path)
    monkeypatch.setattr(seo, "settings", replace(seo.settings, app_base_url="https://hoopcuts.example"))
    with TestClient(main.app) as client:
        yield client


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms", "/refunds", "/support"])
def test_public_metadata_and_content_are_in_initial_html(site, path):
    response = site.get(path, headers={"Host": "untrusted.example"})
    assert response.status_code == 200
    assert '<h1>Public content</h1>' in response.text
    assert response.headers["x-robots-tag"].startswith("index, follow")
    assert response.text.count('rel="canonical"') == 1
    assert f'href="https://hoopcuts.example{path}"' in response.text
    assert "untrusted.example" not in response.text
    assert "https://highlights-web-5xkikaixda-ew.a.run.app" not in response.text
    title = re.search(r"<title>(.*?)</title>", response.text).group(1)
    assert title == json.loads((Path(__file__).resolve().parents[1] / "frontend/public/seo.json").read_text())[path]["title"]
    schema = json.loads(re.search(r'<script id="seo-schema"[^>]*>(.*?)</script>', response.text).group(1))
    assert schema["@graph"][1]["url"] == "https://hoopcuts.example" + path
    assert "aggregateRating" not in json.dumps(schema)


@pytest.mark.parametrize("path", ["/sign-in", "/account", "/library", "/demo", "/runs/private-game"])
def test_private_routes_are_noindex_without_canonical_or_schema(site, path):
    response = site.get(path)
    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "noindex, follow"
    assert '<meta name="robots" content="noindex, follow">' in response.text
    assert 'rel="canonical"' not in response.text
    assert 'id="seo-schema"' not in response.text
    assert "Public content" not in response.text


def test_sitemap_and_robots_share_origin_and_only_public_routes(site):
    response = site.get("/sitemap.xml")
    assert response.status_code == 200
    root = ElementTree.fromstring(response.content)
    urls = [node.text for node in root.findall("{*}url/{*}loc")]
    assert set(urls) == {"https://hoopcuts.example" + path for path in ["/", "/privacy", "/terms", "/refunds", "/support"]}
    robots = site.get("/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap: https://hoopcuts.example/sitemap.xml" in robots.text
    assert "Disallow: /api/" in robots.text
    assert "Disallow: /sign-in" not in robots.text  # Crawlers must see noindex.


def test_unknown_routes_return_404_and_pricing_redirects(site):
    response = site.get("/does-not-exist")
    assert response.status_code == 404
    assert response.headers["x-robots-tag"] == "noindex, follow"
    response = site.get("/pricing", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/#pricing"
