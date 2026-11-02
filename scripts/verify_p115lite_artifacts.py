#!/usr/bin/env python3
"""Require both installation orders to prove the same ZIP, source and tag target."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile


class ArtifactError(Exception):
    pass


def verify(first: Path, second: Path, expected_base: str):
    reports = []
    for directory in (first, second):
        report = json.loads((directory / "compatibility.json").read_text())
        version = report["version"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ArtifactError("Invalid artifact version")
        if (not re.fullmatch(r"[0-9a-f]{40}", report["upstream"])
                or report["base"] != expected_base or not re.fullmatch(r"[0-9a-f]{40}", expected_base)):
            raise ArtifactError("Artifact repository commit does not match the fixed tag target")
        if report["archive"] != f"p115lite_v{version}.zip":
            raise ArtifactError("Invalid artifact filename")
        path = directory / report["archive"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != report["sha256"]:
            raise ArtifactError("Artifact ZIP digest differs from the tested report")
        with zipfile.ZipFile(path) as archive:
            provenance = json.loads(archive.read("UPSTREAM.json"))
        if provenance["commit"] != report["upstream"] or provenance["version"] != version:
            raise ArtifactError("ZIP provenance differs from the tested report")
        reports.append(report)
    if reports[0] != reports[1]:
        raise ArtifactError("Installation orders tested different snapshots/packages")
    return reports[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--expected-base", required=True)
    args = parser.parse_args()
    report = verify(args.first, args.second, args.expected_base)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for key in ("base", "version", "upstream"):
                print(f"{key}={report[key]}", file=output)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
