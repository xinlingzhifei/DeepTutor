from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "deploy" / "nginx" / "yfeistai-classroom.conf"
CHECK_DOCKERFILE = ROOT / "deploy" / "nginx" / "Dockerfile.check"


def _config() -> str:
    return CONFIG.read_text(encoding="utf-8")


def test_openmaic_is_not_publicly_proxied() -> None:
    config = _config().lower()

    assert "proxy_pass http://openmaic" not in config
    assert "location /openmaic" not in config
    assert "openmaic" not in config


def test_http_only_redirects_to_https_and_tls_uses_operator_secrets() -> None:
    config = _config()

    assert "listen 80;" in config
    assert "return 308 https://$host$request_uri;" in config
    assert "listen 443 ssl;" in config
    assert "ssl_certificate /run/secrets/gateway_fullchain.pem;" in config
    assert "ssl_certificate_key /run/secrets/gateway_private_key.pem;" in config


def test_only_yfeistai_frontend_and_api_are_proxied() -> None:
    config = _config()

    assert "location /api/" in config
    assert "resolver 127.0.0.11 valid=30s ipv6=off;" in config
    assert "set $deeptutor_api http://deeptutor:8001;" in config
    assert "proxy_pass $deeptutor_api;" in config
    assert "location /ws/" in config
    assert "location /" in config
    assert "set $deeptutor_web http://deeptutor:3782;" in config
    assert "proxy_pass $deeptutor_web;" in config
    assert "location = /internal/metrics" in config
    assert "return 404;" in config


def test_upload_download_and_private_cache_contract_is_explicit() -> None:
    config = _config()

    assert "client_max_body_size 100m;" in config
    assert "proxy_request_buffering off;" in config
    assert "proxy_buffering off;" in config
    assert "proxy_read_timeout 3600s;" in config
    assert 'add_header Cache-Control "private, no-store" always;' in config


def test_websocket_upgrade_and_forwarded_identity_are_preserved() -> None:
    config = _config()

    assert "proxy_set_header Upgrade $http_upgrade;" in config
    assert "proxy_set_header Connection $connection_upgrade;" in config
    assert "proxy_set_header Host $host;" in config
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in config
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in config


def test_tls_responses_include_required_security_headers() -> None:
    config = _config()

    assert (
        'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
        in config
    )
    assert 'add_header X-Content-Type-Options "nosniff" always;' in config


def test_nginx_check_image_has_no_floating_default() -> None:
    dockerfile = CHECK_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG NGINX_IMAGE\n" in dockerfile
    assert "ARG NGINX_IMAGE=" not in dockerfile
    assert "FROM ${NGINX_IMAGE}" in dockerfile
    assert "COPY yfeistai-classroom.conf /etc/nginx/conf.d/default.conf" in dockerfile
    assert "apk add" not in dockerfile
    assert "openssl req" not in dockerfile
    assert "s/listen 443 ssl;/listen 443;/" in dockerfile
    assert "'/ssl_certificate /d'" in dockerfile
    assert "'/ssl_certificate_key /d'" in dockerfile
    assert "&& nginx -t" in dockerfile
