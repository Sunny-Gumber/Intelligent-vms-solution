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
