from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ARCHITECTURE_PATH = Path("ARCHITECTURE.md")
WORLD_CLASS_PATH = Path("docs/WORLD_CLASS_AUTONOMOUS_UPGRADE.md")
DOCTOR_PATH = Path(".ai/runtime/src/ai_core/doctor.py")
EVAL_RUNNER_PATH = Path(".ai/evals/run.py")
MAKEFILE_PATH = Path("Makefile")

_DOCTOR_MARKER_RE = re.compile(r"<!-- code-brain-contract: doctor-check-count=(\d+) -->")
_EVAL_MARKER_RE = re.compile(r"<!-- code-brain-contract: eval-axes=([A-Za-z0-9_,-]+) -->")
_MAKE_EVAL_RE = re.compile(r"(?ms)^eval:\s*\n(?P<body>(?:\t[^\n]*\n?)+)")
_MAKE_AXIS_RE = re.compile(r"--axis\s+([A-Za-z0-9_-]+)")


class DocsContractSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocsContract:
    doctor_check_count: int
    eval_axes: tuple[str, ...]

    @property
    def doctor_marker(self) -> str:
        return f"<!-- code-brain-contract: doctor-check-count={self.doctor_check_count} -->"

    @property
    def eval_marker(self) -> str:
        return "<!-- code-brain-contract: eval-axes=" + ",".join(self.eval_axes) + " -->"

    @property
    def eval_sentence(self) -> str:
        formatted = ", ".join(f"`{axis}`" for axis in self.eval_axes)
        return f"현재 `make eval`의 강제 축({len(self.eval_axes)}개)은 {formatted}이다."


def _parse_python(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise DocsContractSourceError(f"cannot parse {path}: {exc}") from exc


def _doctor_check_count(path: Path) -> int:
    module = _parse_python(path)
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "run_checks":
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.List):
                continue
            if any(isinstance(target, ast.Name) and target.id == "checks" for target in statement.targets):
                if not statement.value.elts:
                    raise DocsContractSourceError(f"{path}: run_checks checks list is empty")
                return len(statement.value.elts)
    raise DocsContractSourceError(f"{path}: cannot locate run_checks checks list")


def _eval_adapter_axes(path: Path) -> tuple[str, ...]:
    module = _parse_python(path)
    for node in module.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name) or target.id != "ADAPTERS" or not isinstance(value, ast.Dict):
            continue
        axes: list[str] = []
        for key in value.keys:
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise DocsContractSourceError(f"{path}: ADAPTERS keys must be literal strings")
            axes.append(key.value)
        if not axes:
            raise DocsContractSourceError(f"{path}: ADAPTERS is empty")
        return tuple(axes)
    raise DocsContractSourceError(f"{path}: cannot locate ADAPTERS")


def _make_eval_axes(path: Path) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DocsContractSourceError(f"cannot read {path}: {exc}") from exc
    match = _MAKE_EVAL_RE.search(text)
    if match is None:
        raise DocsContractSourceError(f"{path}: cannot locate eval target recipe")
    axes = tuple(_MAKE_AXIS_RE.findall(match.group("body")))
    if not axes:
        raise DocsContractSourceError(f"{path}: eval target declares no --axis values")
    return axes


def load_source_contract(root: Path) -> DocsContract:
    root = root.resolve()
    doctor_count = _doctor_check_count(root / DOCTOR_PATH)
    adapter_axes = _eval_adapter_axes(root / EVAL_RUNNER_PATH)
    make_axes = _make_eval_axes(root / MAKEFILE_PATH)
    if adapter_axes != make_axes:
        raise DocsContractSourceError(
            "eval source drift: .ai/evals/run.py ADAPTERS="
            + ",".join(adapter_axes)
            + " Makefile:eval="
            + ",".join(make_axes)
        )
    return DocsContract(doctor_check_count=doctor_count, eval_axes=adapter_axes)


def _validate_single_marker(
    *,
    text: str,
    pattern: re.Pattern[str],
    expected: str,
    label: str,
    issues: list[str],
) -> None:
    matches = pattern.findall(text)
    if len(matches) != 1:
        issues.append(f"{label}: expected exactly one contract marker, found {len(matches)}")
        return
    if expected not in text:
        issues.append(f"{label}: contract marker drift; expected {expected}")


def validate_document_texts(
    contract: DocsContract,
    *,
    architecture_text: str,
    world_class_text: str,
) -> list[str]:
    issues: list[str] = []
    _validate_single_marker(
        text=architecture_text,
        pattern=_DOCTOR_MARKER_RE,
        expected=contract.doctor_marker,
        label=str(ARCHITECTURE_PATH),
        issues=issues,
    )
    _validate_single_marker(
        text=world_class_text,
        pattern=_DOCTOR_MARKER_RE,
        expected=contract.doctor_marker,
        label=str(WORLD_CLASS_PATH),
        issues=issues,
    )
    _validate_single_marker(
        text=world_class_text,
        pattern=_EVAL_MARKER_RE,
        expected=contract.eval_marker,
        label=str(WORLD_CLASS_PATH),
        issues=issues,
    )

    if f"{contract.doctor_check_count} checks" not in architecture_text:
        issues.append(
            f"{ARCHITECTURE_PATH}: human-readable doctor count must say {contract.doctor_check_count} checks"
        )
    if f"{contract.doctor_check_count}개 check" not in world_class_text:
        issues.append(
            f"{WORLD_CLASS_PATH}: human-readable doctor count must say {contract.doctor_check_count}개 check"
        )
    if contract.eval_sentence not in world_class_text:
        issues.append(f"{WORLD_CLASS_PATH}: eval axis sentence drift; expected {contract.eval_sentence}")
    return issues


def validate_docs_contract(root: Path, contract: DocsContract | None = None) -> list[str]:
    root = root.resolve()
    active_contract = contract or load_source_contract(root)
    try:
        architecture_text = (root / ARCHITECTURE_PATH).read_text(encoding="utf-8")
        world_class_text = (root / WORLD_CLASS_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read contract documentation: {exc}"]
    return validate_document_texts(
        active_contract,
        architecture_text=architecture_text,
        world_class_text=world_class_text,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify docs against runtime/eval source inventories.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        contract = load_source_contract(args.root)
        issues = validate_docs_contract(args.root, contract)
    except DocsContractSourceError as exc:
        print(f"docs contract source error: {exc}", file=sys.stderr)
        return 1
    if issues:
        for issue in issues:
            print(f"docs contract drift: {issue}", file=sys.stderr)
        return 1
    print(
        f"docs contract ok: doctor_checks={contract.doctor_check_count} "
        f"eval_axes={','.join(contract.eval_axes)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())