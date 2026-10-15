"""Reviewable source test inventory and ignored-test schedules.

This lexical/AST inventory detects handwritten entry-point drift; it does not
expand Rust macros, resolve cfg, discover generated tests, or prove execution.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess

from .common import ROOT, EvidenceError, digest, read_json

SKIP_DIRS = {".git", "target", "results", "__pycache__", ".venv", ".qa-venv", "venv", "node_modules"}
RUST_TEST_ATTRIBUTES = {"test", "tokio::test", "test_case"}
RUST_RECORDED_ATTRIBUTES = RUST_TEST_ATTRIBUTES | {"ignore", "cfg", "cfg_attr", "should_panic"}
RAW = re.compile(r'(?:br|cr|r)(#{0,255})"')
QUOTED = re.compile(r'(?:b|c)?"')
CHAR = re.compile(r"b?'(?:\\(?:u\{[0-9a-fA-F_]+\}|.)|[^'\\\r\n])'")
IDENT = re.compile(r"(?:r#)?[^\W\d]\w*", re.UNICODE)
SCOPE = {"rust": "Handwritten #[test], #[tokio::test], #[test_case] functions in repository .rs sources.",
         "rust_configuration": "All cfg/cfg_attr attribute text, including module/file capability gates; no cfg evaluation.",
         "python": "Module/class test* functions in repository test_*.py sources, including decorators.",
         "excluded_directories": sorted(SKIP_DIRS),
         "source_selection": "Git root: cached and nonignored untracked sources; nonrepository fixture: directory walk.",
         "limitations": "Source inventory only: no Rust macro expansion/cfg resolution, generated tests, runtime discovery or execution claim."}


def _tokens(text):
    """Strip nested comments and tokenize literals atomically; never parse their contents."""
    tokens, index = [], 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end + 1
            continue
        if text.startswith("/*", index):
            depth, index = 1, index + 2
            while depth and index < len(text):
                if text.startswith("/*", index): depth, index = depth + 1, index + 2
                elif text.startswith("*/", index): depth, index = depth - 1, index + 2
                else: index += 1
            if depth: raise EvidenceError("unterminated Rust block comment in test inventory")
            continue
        raw = RAW.match(text, index)
        if raw:
            end = text.find('"' + raw[1], index + len(raw[0]))
            if end < 0: raise EvidenceError("unterminated Rust raw string in test inventory")
            end += len(raw[1]) + 1
            tokens.append(text[index:end]); index = end
            continue
        quoted = QUOTED.match(text, index)
        if quoted:
            end = index + len(quoted[0])
            while end < len(text):
                if text[end] == "\\": end += 2
                elif text[end] == '"': end += 1; break
                else: end += 1
            else: raise EvidenceError("unterminated Rust string in test inventory")
            tokens.append(text[index:end]); index = end
            continue
        char = CHAR.match(text, index)
        if char:
            tokens.append(char[0]); index += len(char[0]); continue
        ident = IDENT.match(text, index)
        if ident:
            tokens.append(ident[0]); index += len(ident[0]); continue
        tokens.append(text[index]); index += 1
    return tokens


def rust_tests(text, source):
    tokens, entries, pending, occurrences, index = _tokens(text), [], [], Counter(), 0
    while index < len(tokens):
        if tokens[index:index + 2] == ["#", "["]:
            depth, end = 1, index + 2
            while depth and end < len(tokens):
                if tokens[end] == "[": depth += 1
                elif tokens[end] == "]": depth -= 1
                end += 1
            if depth: raise EvidenceError("unterminated Rust attribute in test inventory")
            attr = tokens[index + 2:end - 1]
            head = []
            for token in attr:
                if token in ("(", "="): break
                head.append(token)
            pending.append(("".join(head), attr))
            index = end
            continue
        token = tokens[index]
        if token == "pub" and tokens[index + 1:index + 2] == ["("]:
            end = index + 2
            while end < len(tokens) and tokens[end] != ")": end += 1
            if end == len(tokens): raise EvidenceError("unterminated Rust visibility qualifier")
            index = end + 1
            continue
        if token == "fn" and index + 1 < len(tokens):
            name = tokens[index + 1]
            if any(head in RUST_TEST_ATTRIBUTES for head, _ in pending):
                if not re.fullmatch(r"(?:r#)?[^\W\d]\w*", name, re.UNICODE):
                    raise EvidenceError("test function name is generated/unsupported; register a handwritten entry")
                occurrences[name] += 1
                entries.append({"source": source, "name": name, "occurrence": occurrences[name],
                    "attributes": [" ".join(attr) for head, attr in pending if head in RUST_RECORDED_ATTRIBUTES],
                    "ignored": any(head == "ignore" for head, _ in pending)})
            pending = []
        elif token not in ("pub", "async", "unsafe", "extern", "default", "const") and not token.startswith('"'):
            pending = []
        index += 1
    return entries


def rust_configuration(text):
    tokens, attributes, index = _tokens(text), [], 0
    while index < len(tokens):
        start = index + (2 if tokens[index:index + 2] == ["#", "!"] else 1)
        if tokens[index] != "#" or tokens[start:start + 1] != ["["]:
            index += 1
            continue
        end, depth = start + 1, 1
        while depth and end < len(tokens):
            if tokens[end] == "[": depth += 1
            elif tokens[end] == "]": depth -= 1
            end += 1
        if depth: raise EvidenceError("unterminated Rust configuration attribute")
        attr = tokens[start + 1:end - 1]
        if attr and attr[0] in ("cfg", "cfg_attr"):
            attributes.append(" ".join(attr))
        index = end
    return attributes


def python_tests(text, source):
    try: tree = ast.parse(text, filename=source)
    except SyntaxError as error: raise EvidenceError("invalid Python test source: " + source) from error
    entries, occurrences = [], Counter()
    def visit(body, classes=(), decorators=(), guards=()):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, (*classes, node.name), (*decorators, *(ast.unparse(d) for d in node.decorator_list)), guards)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                key = (*classes, node.name)
                occurrences[key] += 1
                entries.append({"source": source, "class": ".".join(classes), "name": node.name,
                    "occurrence": occurrences[key], "async": isinstance(node, ast.AsyncFunctionDef),
                    "decorators": [*decorators, *(ast.unparse(d) for d in node.decorator_list)], "guards": list(guards)})
            elif isinstance(node, ast.If):
                visit(node.body, classes, decorators, (*guards, "if " + ast.unparse(node.test)))
                visit(node.orelse, classes, decorators, (*guards, "else " + ast.unparse(node.test)))
            elif isinstance(node, ast.Try):
                visit(node.body, classes, decorators, (*guards, "try"))
                for handler in node.handlers:
                    visit(handler.body, classes, decorators, (*guards, "except " + (ast.unparse(handler.type) if handler.type else "all")))
                visit(node.orelse, classes, decorators, (*guards, "try-else"))
                visit(node.finalbody, classes, decorators, (*guards, "finally"))
    visit(tree.body)
    return entries


def _sources(root):
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                             capture_output=True, text=True, timeout=10)
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
            result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
                                    capture_output=True, text=True, timeout=10, check=True)
            return sorted({root / name for name in result.stdout.split("\0") if name})
        if (root / ".git").exists():
            raise EvidenceError("cannot identify source Git root for inventory")
    except (OSError, subprocess.SubprocessError) as error:
        if (root / ".git").exists():
            raise EvidenceError("cannot enumerate source Git inventory") from error
    paths = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in SKIP_DIRS and not (Path(directory) / name).is_symlink())
        paths.extend(Path(directory) / name for name in sorted(files))
    return paths


def test_inventory(root):
    root = Path(root)
    rust, configuration, python = [], [], []
    for path in _sources(root):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts): continue
        if path.suffix != ".rs" and not (path.name.startswith("test_") and path.suffix == ".py"): continue
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise EvidenceError("test inventory refuses symlink source: " + str(path))
        source = relative.as_posix()
        if path.suffix == ".rs":
            text = path.read_text()
            rust.extend(rust_tests(text, source))
            attributes = rust_configuration(text)
            if attributes: configuration.append({"source": source, "attributes": attributes})
        else: python.extend(python_tests(path.read_text(), source))
    return {"schema_version": 1, "scope": SCOPE, "rust_tests": sorted(rust, key=lambda e: (e["source"], e["name"], e["occurrence"])),
            "rust_configuration": sorted(configuration, key=lambda e: e["source"]),
            "python_tests": sorted(python, key=lambda e: (e["source"], e["class"], e["name"], e["occurrence"]))}


def _audit_record(inventory, registry):
    return {"ok": True, "registered": len(registry), "tests": registry, "test_inventory_hash": digest(inventory),
            "counts": {"rust": len(inventory["rust_tests"]), "python": len(inventory["python_tests"]),
                       "rust_configuration_files": len(inventory["rust_configuration"])}}


def verify_audit(value, policy_dir):
    """Reporter proof must bind the reviewed inventory, not just say coverage ok."""
    policy_dir = Path(policy_dir)
    expected = _audit_record(read_json(policy_dir / "test-inventory.json"), read_json(policy_dir / "ignored.json")["tests"])
    if (not isinstance(value, dict) or value.get("ok") is not True or type(value.get("registered")) is not int
            or not isinstance(value.get("counts"), dict) or any(type(v) is not int for v in value["counts"].values())
            or value != expected):
        raise EvidenceError("coverage audit differs from frozen test inventory/ignored registry")
    return value


def audit(root, policy_dir=None):
    root, policy_dir = Path(root), Path(policy_dir or Path(root) / "qa/policy")
    inventory = test_inventory(root)
    expected_inventory = read_json(policy_dir / "test-inventory.json")
    if inventory != expected_inventory:
        changed = [kind for kind in ("rust_tests", "rust_configuration", "python_tests") if inventory[kind] != expected_inventory.get(kind)]
        raise EvidenceError("test inventory drift requires explicit policy review: " + ", ".join(changed or ["schema/scope"]))
    found = sorted((entry["name"], entry["source"]) for entry in inventory["rust_tests"] if entry["ignored"])
    registry = read_json(policy_dir / "ignored.json")["tests"]
    expected = sorted((entry["name"].split("::")[-1], entry["source"]) for entry in registry)
    if found != expected:
        raise EvidenceError("ignored coverage drift: registered names/source/occurrences differ")
    for entry in registry:
        if entry["category"] not in ("heavy_correctness", "soak", "diagnostic", "benchmark"):
            raise EvidenceError("unknown ignored category")
        if not entry["schedule"] or not entry["reason"]:
            raise EvidenceError("ignored test needs an explicit schedule and reason")
    return _audit_record(inventory, registry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True, help="Explicit review candidate path; never updated during audit")
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(test_inventory(args.root), indent=2, ensure_ascii=False, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
