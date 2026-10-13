"""Build the one code bundle all Lambdas share: the ``freight`` package plus its dependencies,
as Linux arm64 wheels.

uv resolves manylinux aarch64 wheels on any host, so no Docker is needed. The build is skipped
when neither uv.lock nor any file under src/ changed since the last build.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "infra" / "build" / "lambda"
STAMP = OUT.parent / "lambda.sha256"
PYTHON_VERSION = "3.12"
PLATFORM = "aarch64-manylinux2014"


def _fingerprint() -> str:
    h = hashlib.sha256()
    for path in sorted([ROOT / "uv.lock", ROOT / "pyproject.toml", *(ROOT / "src").rglob("*")]):
        if path.is_file() and "__pycache__" not in path.parts:
            h.update(str(path.relative_to(ROOT)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def build_lambda_bundle(force: bool = False) -> Path:
    fingerprint = _fingerprint()
    if not force and OUT.exists() and STAMP.exists() and STAMP.read_text() == fingerprint:
        return OUT
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    requirements = OUT.parent / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-group",
            "infra",
            "--no-hashes",
            "-o",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(OUT),
            "--python-platform",
            PLATFORM,
            "--python-version",
            PYTHON_VERSION,
            "--only-binary",
            ":all:",
            "--no-deps",  # the exported lock is already the full, pinned closure
            "-r",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    shutil.copytree(
        ROOT / "src" / "freight", OUT / "freight", ignore=shutil.ignore_patterns("__pycache__")
    )
    # boto3/botocore ship with the Lambda runtime but anthropic[bedrock] pins its own; keep ours
    # so local tests and Lambda run the same versions.
    STAMP.write_text(fingerprint)
    return OUT


if __name__ == "__main__":
    print(build_lambda_bundle(force=True))
