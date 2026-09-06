"""Verify the pinned upstream backend branding diff with Python's standard library.

Run: python integrations/ragflow/branding/backend-contract.py
This checks source contracts and syntax; it does not compile Go or start services.
"""

import ast
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

UPSTREAM = Path(__file__).resolve().parents[1] / "upstream"
PIN = "b9df87c4c75a5b0d35c90d15329fc0f6f91cb73e"
BRAND = re.compile(r"\bRAGFlow\b")
TEXT_LINES = {
    "api/utils/web_utils.py": [207],
    "internal/utility/smtp.go": [79],
    "internal/server/config/smtp_config.go": [33],
    "api/channels/dingtalk/channel.py": [92],
    "internal/channels/dingtalk.go": [200],
    "api/channels/whatsapp/gateway-node/index.js": [210],
    "api/apps/restful_apis/aimlapi_api.py": [60, 91],
    "admin/client/ragflow_cli.py": [247, 256, 257, 258],
    "cmd/ragflow-cli.go": [44],
    "internal/cli/cli.go": [441, 446, 458, 459, 709, 717, 719, 802],
    "internal/cli/filesystem/skill_install.go": [380, 408],
    "internal/cli/filesystem/skill_uninstall.go": [152],
    "api/ragflow_server.py": [17, 101, 120, 173],
    "admin/server/admin_server.py": [60, 82],
    "rag/svr/task_executor.py": [1938, 1957],
    "rag/svr/sync_data_source.py": [2320, 2329],
    "cmd/ragflow_server.go": [164, 181, 190, 200, 210, 237, 482, 486, 496, 600, 616, 651, 667, 954],
    "mcp/server/server.py": [518, 526, 544, 606, 629, 797, 798, 799],
    "internal/mcp/server.go": [152, 224, 246],
    "agent/sandbox/providers/ssh.py": [320],
    "agent/sandbox/providers/self_managed.py": [249, 260],
    "agent/component/browser.py": [652],
    "agent/sandbox/executor_manager/services/auth.py": [29],
    "internal/admin/handler.go": [238],
    "common/doc_store/gaussdb_conn_base.py": [1657],
    "pyproject.toml": [4],
    "admin/client/pyproject.toml": [4],
    "helm/Chart.yaml": [3],
    "agent/templates/cajal_scientific_paper_agent.json": [],
    "agent/templates/web_search_assistant.json": [],
    "agent/templates/seo_article_writer.json": [],
    "agent/templates/market_seo_article_writer.json": [],
}
EXTRA_EDITS = {
    "admin/client/ragflow_cli.py": [
        {"start": 72, "end": 73, "lines": ['    prompt = "yFei> "']},
        {"start": 237, "end": 244, "lines": ['            print("\\n=== yFei CLI ===\\n")']},
    ],
    "api/ragflow_server.py": [
        {"start": 92, "end": 100, "lines": ['    logging.info("\\n=== yFei Server ===\\n")']}
    ],
    "admin/server/admin_server.py": [
        {"start": 45, "end": 52, "lines": ['    logging.info("\\n=== yFei Admin ===\\n")']}
    ],
    "cmd/ragflow_server.go": [
        {"start": 472, "end": 473, "lines": ["\t// Print yFei Admin banner"]},
        {"start": 473, "end": 479, "lines": ['\tcommon.Info("\\n=== yFei Admin ===\\n")']},
        {"start": 946, "end": 953, "lines": ['\t\tcommon.Info("\\n=== yFei Server ===\\n")']},
    ],
}


class HideStringValues(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            node.value = "<string>"
        return node


def structural_python(source, filename):
    tree = ast.parse(source, filename=filename)
    return ast.dump(HideStringValues().visit(tree), include_attributes=False)


def baseline(filename):
    result = subprocess.run(
        ["git", "-C", str(UPSTREAM), "show", f"{PIN}:{filename}"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8").replace("\r\n", "\n")


def expected_text(filename, original):
    lines = original.split("\n")
    numbers = TEXT_LINES[filename]
    if filename.startswith("agent/templates/"):
        numbers = [i + 1 for i, line in enumerate(lines) if BRAND.search(line)]
    edits = []
    for number in numbers:
        line = lines[number - 1]
        if not BRAND.search(line):
            raise AssertionError(f"Pin mismatch: {filename}:{number}")
        edits.append(
            {
                "start": number - 1,
                "end": number,
                "lines": [BRAND.sub("yFei", line)],
            }
        )
    edits.extend(EXTRA_EDITS.get(filename, []))
    for edit in sorted(edits, key=lambda item: item["start"], reverse=True):
        lines[edit["start"] : edit["end"]] = edit["lines"]
    return "\n".join(lines)


def main():
    pin = json.loads((UPSTREAM.parent / "UPSTREAM.json").read_text(encoding="utf-8"))
    head = subprocess.check_output(
        ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True
    ).strip()
    if pin["commit"] != PIN or head != PIN:
        raise AssertionError("Upstream pin changed; revalidate branding before upgrading")
    counts = Counter()
    for filename in TEXT_LINES:
        original = baseline(filename)
        current = (UPSTREAM / filename).read_text(encoding="utf-8")
        expected = expected_text(filename, original)
        # apply_patch adds the missing EOF newline in two upstream JSON files.
        if current.rstrip("\n") != expected.rstrip("\n"):
            raise AssertionError(f"Unapproved non-brand source change: {filename}")
        suffix = Path(filename).suffix
        if suffix == ".py":
            if structural_python(original, filename) != structural_python(current, filename):
                raise AssertionError(f"Non-string Python AST changed: {filename}")
        elif suffix == ".json":
            if json.loads(current) != json.loads(expected):
                raise AssertionError(f"Template contract changed: {filename}")
        elif suffix == ".toml":
            before, after = tomllib.loads(original), tomllib.loads(current)
            expected_description = BRAND.sub("yFei", before["project"]["description"])
            if after["project"]["description"] != expected_description:
                raise AssertionError(f"Package description mismatch: {filename}")
            before["project"].pop("description")
            after["project"].pop("description")
            if before != after:
                raise AssertionError(f"Package metadata changed: {filename}")
        counts[suffix] += 1
    print(
        json.dumps(
            {
                "status": "passed",
                "upstream_pin": PIN,
                "files": sum(counts.values()),
                "by_extension": dict(sorted(counts.items())),
                "python": "syntax parsed; AST identical after string-value normalization",
                "templates": "JSON parsed; only declared natural-language branding changed",
                "packages": "TOML parsed; only project.description changed",
                "go": "pinned source matches declared string/banner edits; NOT compiled",
                "helm": "pinned file matches the description-only edit",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
