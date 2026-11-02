from pathlib import Path


ROOT = Path(__file__).parents[1]
WEB_DOCKERFILE = (ROOT / "services" / "web" / "Dockerfile").read_text(encoding="utf-8")


def test_web_image_refreshes_security_packages_before_non_root_runtime():
    """Keep the web image security refresh ahead of its non-root runtime boundary."""
    lines = [line.strip() for line in WEB_DOCKERFILE.splitlines() if line.strip()]
    upgrade_index = lines.index("RUN apk upgrade --no-cache")
    user_index = lines.index("USER 101:101")

    assert lines[0].startswith("FROM nginx:")
    assert upgrade_index < user_index
    assert "USER root" not in WEB_DOCKERFILE


def test_web_image_pins_patched_nginx_release_and_verified_registry_digest():
    """Prevent rollback to the nginx 1.30.5 HIGH-CVE baseline.

    The manifest-list digest was read from the official library/nginx registry
    metadata for 1.30.6-alpine3.24-slim before changing the build input.
    """
    assert WEB_DOCKERFILE.splitlines()[0] == (
        "FROM nginx:1.30.6-alpine3.24-slim@sha256:"
        "dede8895648b461178c3bc604f7eed4423efe5220b2c4d839f5eb3d34ac3f8f7"
    )
