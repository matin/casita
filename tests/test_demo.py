import shutil
import importlib.util
import re
from urllib.request import urlopen

import casita
from casita import html, listing_page
from casita.models import Listing


PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def test_demo_fixture_renders_offline(tmp_path, monkeypatch):
    fixture = casita.DEMO_FIXTURE
    db_path = tmp_path / "demo.sqlite"
    output_dir = tmp_path / "site"
    shutil.copy2(fixture, db_path)

    monkeypatch.setenv("CASITA_DB_PATH", str(db_path))
    monkeypatch.setenv("CASITA_ROUTE_CACHE_DB", str(db_path))
    monkeypatch.setenv("CASITA_ROUTES_OFFLINE", "1")
    monkeypatch.setenv("CASITA_SITE_URL", "http://127.0.0.1:8765")

    result = casita._render_site("index.html", output_dir)

    assert result["listings"] > 100
    assert result["details"] == result["listings"]
    assert result["og_images"] == result["details"] + 1
    assert result["out_html"].exists()
    assert (output_dir / "og" / "index.png").read_bytes().startswith(PNG_HEADER)
    listing_pages = list((output_dir / "listing").glob("*.html"))
    assert len(listing_pages) == result["details"]
    first_listing = listing_pages[0]
    assert f"/og/listing/{first_listing.stem}.png" in first_listing.read_text()
    assert (output_dir / "og" / "listing" / f"{first_listing.stem}.png").read_bytes().startswith(PNG_HEADER)
    assert "/og/index.png" in result["out_html"].read_text()
    assert (output_dir / "assets" / "favicon.svg").exists()

    local_refs = []
    for page in [result["out_html"], *listing_pages]:
        for match in re.finditer(r"""(?:src|href)=["']([^"']+)["']""", page.read_text()):
            url = match.group(1)
            if url.startswith("/") and not url.startswith("//"):
                local_refs.append((page, url))
    missing = [
        f"{page.relative_to(output_dir)} -> {url}"
        for page, url in local_refs
        if not (output_dir / url.lstrip("/")).exists()
        and not (output_dir / f"{url.lstrip('/')}.html").exists()
    ]
    assert missing == []

    with casita._serve_rendered_site(output_dir) as base_url:
        with urlopen(f"{base_url}/", timeout=5) as response:
            assert response.status == 200
        with urlopen(f"{base_url}/listing/{first_listing.stem}", timeout=5) as response:
            assert response.status == 200
        with urlopen(f"{base_url}/og/listing/{first_listing.stem}.png", timeout=5) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "image/png"


def test_package_fixture_matches_repo_fixture():
    assert casita.DEMO_FIXTURE.read_bytes() == (
        casita.ROOT / "fixtures" / "demo.sqlite"
    ).read_bytes()


def test_default_scrub_redacts_contact_info_without_matching_coordinates():
    phone = "415" + "-555" + "-1212"
    text = listing_page._scrub(f"Call {phone} or leasing@example.com")
    assert text == "Call [redacted] or [redacted]"
    assert listing_page._scrub("37.956-122.3933") == "37.956-122.3933"


def test_index_open_graph_urls_are_escaped(monkeypatch):
    monkeypatch.setenv("CASITA_SITE_URL", 'https://example.test/"bad')
    rendered = html.render(
        [Listing(
            source="manual",
            source_id="1",
            url="",
            title="Demo listing",
            address="1 Demo St",
            neighborhood="demo",
            price=1000,
            beds=1,
            baths=1,
            dog_policy="dogs_ok",
            llm_severity="ok",
        )],
        run={"started_at": "2026-01-01T00:00:00", "finished_at": "2026-01-01T00:00:00"},
    )

    assert 'content="https://example.test/&quot;bad/og/index.png"' in rendered


def test_demo_clean_url_path_resolves_listing_html(tmp_path):
    listing = tmp_path / "listing" / "sample-listing.html"
    listing.parent.mkdir()
    listing.write_text("<h1>Sample listing</h1>")

    resolved = casita._demo_clean_url_path(
        "/listing/sample-listing",
        tmp_path / "listing" / "sample-listing",
    )

    assert resolved == str(listing)


def test_rendered_site_server_serves_clean_urls_and_assets(tmp_path):
    (tmp_path / "index.html").write_text("home")
    listing = tmp_path / "listing" / "sample-listing.html"
    listing.parent.mkdir()
    listing.write_text("detail")
    image = tmp_path / "og" / "index.png"
    image.parent.mkdir()
    image.write_bytes(PNG_HEADER + b"demo")

    with casita._serve_rendered_site(tmp_path) as base_url:
        with urlopen(f"{base_url}/", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"home"
        with urlopen(f"{base_url}/listing/sample-listing", timeout=5) as response:
            assert response.status == 200
            assert response.read() == b"detail"
        with urlopen(f"{base_url}/og/index.png", timeout=5) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "image/png"
            assert response.read().startswith(PNG_HEADER)


def test_public_validator_passes():
    path = casita.ROOT / "scripts" / "validate_public.py"
    spec = importlib.util.spec_from_file_location("validate_public", path)
    assert spec and spec.loader
    validate_public = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validate_public)

    validate_public.main()
