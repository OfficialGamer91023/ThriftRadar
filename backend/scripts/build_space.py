"""Assemble the minimal bundle that is published as the Hugging Face Space. Spec: DESIGN.md §4.10 (step 15).

Run from backend/:  uv run python -m scripts.build_space [--out ../dist/space]
Copies only what the image needs, writes backend/requirements.txt (CPU torch; no CUDA packages), then refuses to
finish if the bundle holds anything private: env files, auth or data dirs, phone-like numbers, WhatsApp ids or
key-like strings. Nothing is uploaded; pushing the bundle is a separate, explicit step.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "dist" / "space"
DEPLOY = REPO / "deploy" / "space"
SCRIPTS = ("__init__.py", "bake_models.py", "seed_demo.py")
WEB_SKIP = {"node_modules", ".next", "out", "next-env.d.ts", "tsconfig.tsbuildinfo"}
CUDA = re.compile(r"^(nvidia-|triton|cuda-)")

FORBIDDEN_NAMES = re.compile(r"(^|/)(\.env[^/]*|auth[^/]*|data|exports|spool|state)(/|$)")
PHONE = re.compile(r"(?<![\w./])\+?\d[\d\s\-()]{9,}\d(?![\w.])")
SECRETS = [
    re.compile(r"@s\.whatsapp\.net|@g\.us|wa\.me/"),
    re.compile(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{24,}"),
    re.compile(r"\b(sk|rc|hf)_[A-Za-z0-9]{20,}"),
]
ALLOWED_DIGITS = {"01234567890123456789", "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹"}  # caption.py digit table
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".css", ".md", ".sh", ".sql", ".txt", ".csv", ""}


class BundleError(Exception):
    pass


def requirements() -> str:
    out = subprocess.run(
        ["uv", "export", "--no-dev", "--no-hashes", "--no-emit-project", "--format", "requirements-txt"],
        cwd=REPO / "backend", check=True, capture_output=True, text=True).stdout
    keep = [ln for ln in out.splitlines() if not CUDA.match(ln.strip())]
    return "\n".join(keep) + "\n"


def copy_tree(src: Path, dst: Path, skip: set[str] = frozenset()) -> None:
    shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names
                                                       if n in skip or n == "__pycache__" or n.endswith(".pyc")])


def build(out: Path) -> list[Path]:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in ("Dockerfile", ".dockerignore", "entrypoint.sh", "build_seed.sh", "README.md"):
        shutil.copy2(DEPLOY / name, out / name)
    b = out / "backend"
    copy_tree(REPO / "backend" / "app", b / "app")
    (b / "scripts").mkdir(parents=True)
    for name in SCRIPTS:
        if (REPO / "backend" / "scripts" / name).exists():
            shutil.copy2(REPO / "backend" / "scripts" / name, b / "scripts" / name)
    seed = REPO / "backend" / "seed"
    if seed.is_dir():
        copy_tree(seed, b / "seed")
    else:
        (b / "seed").mkdir()
        (b / "seed" / ".keep").write_text("")
    (b / "requirements.txt").write_text(requirements())
    copy_tree(REPO / "web", out / "web", WEB_SKIP)
    return sorted(p for p in out.rglob("*") if p.is_file())


def check(out: Path, files: list[Path]) -> list[str]:
    problems = []
    for p in files:
        rel = p.relative_to(out).as_posix()
        if FORBIDDEN_NAMES.search(rel) and not rel.startswith("backend/app/"):
            problems.append(f"{rel}: private path")
        if p.suffix.lower() not in TEXT_SUFFIXES or rel.endswith("package-lock.json"):
            continue
        text = p.read_text(errors="replace")
        phones = [m for m in PHONE.finditer(text) if m.group().strip() not in ALLOWED_DIGITS]
        hits = phones[:1] + [m for rx in SECRETS if (m := rx.search(text))]
        if hits:
            problems.append(f"{rel}: suspicious text at offset {hits[0].start()}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.build_space")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    files = build(args.out)
    problems = check(args.out, files)
    if problems:
        print("refusing: the bundle may contain private data:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        return 2
    size = sum(p.stat().st_size for p in files)
    print(f"bundle ready: {args.out} ({len(files)} files, {size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
