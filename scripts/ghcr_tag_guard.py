"""Fail-closed, read-only final-tag admission for the independent RAGFlow build.

Only an authenticated manifest 404/MANIFEST_UNKNOWN proves absence. This GET
is not an atomic create-if-absent operation; the workflow still requires its
shared publication queue, a single authorized writer and post-push digest check.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
import http.client
import json
import os
import re

IMAGE_REPOSITORY = "ghcr.io/xinlingzhifei/yfeistai-ragflow"
TOKEN_PATH = "/token?service=ghcr.io&scope=repository%3Axinlingzhifei%2Fyfeistai-ragflow%3Apull"
MANIFEST_PATH = "/v2/xinlingzhifei/yfeistai-ragflow/manifests/"
MAX_BODY_BYTES = 65_536
MAX_TOKEN_LENGTH = 16_384
ERROR_CODES = frozenset({"MANIFEST_UNKNOWN", "NAME_UNKNOWN", "UNAUTHORIZED", "DENIED", "UNKNOWN"})


class GuardFailure(Exception):
    """Carries only safe diagnostics, never response bodies or credentials."""

    def __init__(self, stage, status=None, codes=()):
        super().__init__("GHCR tag absence is unproven")
        self.stage = stage
        self.status = status
        self.codes = sorted(
            {code for code in codes if isinstance(code, str) and code in ERROR_CODES}
        )

    def diagnostic(self):
        return {"stage": self.stage, "status": self.status, "errorCodes": self.codes}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Non-JSON numeric constant")


def _json_response(stage, path, authorization, expected_status):
    connection = None
    status = None
    try:
        # HTTPSConnection neither follows redirects nor consults Docker/gh auth.
        # The host, token realm, service and pull scope are fixed, not server input.
        connection = http.client.HTTPSConnection("ghcr.io", timeout=20)
        connection.request(
            "GET",
            path,
            headers={
                "Authorization": authorization,
                "Accept": (
                    "application/json"
                    if stage == "token"
                    else "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json,"
                    "application/vnd.oci.image.index.v1+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json"
                ),
            },
        )
        response = connection.getresponse()
        status = response.status
        if status != expected_status:
            raise GuardFailure(stage, status)
        media_type = response.getheader("Content-Type", "") or ""
        if media_type.partition(";")[0].strip().lower() != "application/json":
            raise GuardFailure(stage, status)
        body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise GuardFailure(stage, status)
        document = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(document, dict):
            raise GuardFailure(stage, status)
        return document
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        raise GuardFailure(stage, status) from None
    finally:
        if connection is not None:
            connection.close()


def _safe_token(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_TOKEN_LENGTH
        and all("!" <= character <= "~" for character in value)
    )


def require_absent(*, image_repository, release_tag, actor, github_token):
    """Check the fixed candidate; credentials must come from the caller, not disk."""
    tag = re.fullmatch(r"yfeistai-first-release-([0-9]{8})-([0-9a-f]{8})", release_tag)
    if (
        image_repository != IMAGE_REPOSITORY
        or not tag
        or tag[2] == "00000000"
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?", actor)
        or not _safe_token(github_token)
    ):
        raise GuardFailure("input")
    try:
        datetime.strptime(tag[1], "%Y%m%d")
    except ValueError:
        raise GuardFailure("input") from None
    basic = base64.b64encode(f"{actor}:{github_token}".encode("ascii")).decode("ascii")
    token_document = _json_response("token", TOKEN_PATH, "Basic " + basic, 200)
    token = token_document.get("token", token_document.get("access_token"))
    if (
        set(token_document) - {"token", "access_token", "expires_in", "issued_at"}
        or not _safe_token(token)
        or ("access_token" in token_document and token_document["access_token"] != token)
    ):
        raise GuardFailure("token", 200)
    document = _json_response("manifest", MANIFEST_PATH + release_tag, "Bearer " + token, 404)
    errors = document.get("errors")
    codes = (
        [entry.get("code") for entry in errors if isinstance(entry, dict)]
        if isinstance(errors, list)
        else []
    )
    if (
        set(document) != {"errors"}
        or not isinstance(errors, list)
        or not errors
        or any(
            not isinstance(entry, dict)
            or set(entry) - {"code", "message", "detail"}
            or entry.get("code") != "MANIFEST_UNKNOWN"
            for entry in errors
        )
    ):
        raise GuardFailure("manifest", 404, codes)
    return {"stage": "manifest", "status": 404, "errorCodes": ["MANIFEST_UNKNOWN"]}


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        # argparse's default error includes user-supplied argument values.
        raise GuardFailure("input")


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--image-repository", required=True)
    parser.add_argument("--release-tag", required=True)
    try:
        arguments = parser.parse_args(argv)
        diagnostic = require_absent(
            image_repository=arguments.image_repository,
            release_tag=arguments.release_tag,
            actor=os.environ.get("GITHUB_ACTOR", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
        )
    except GuardFailure as error:
        print(json.dumps(error.diagnostic(), sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(diagnostic, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
