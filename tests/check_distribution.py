"""Run after `uv build`: inspect archives and smoke-test a clean, dependency-free wheel."""
import json
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
wheel = ROOT / "dist/fin_harness-0.1.0-py3-none-any.whl"
sdist = ROOT / "dist/fin_harness-0.1.0.tar.gz"
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    for path in (ROOT / "protocol/v1").rglob("*.json"):
        suffix = "/share/fin-harness/" + path.relative_to(ROOT).as_posix()
        assert any(name.endswith(suffix) for name in names), suffix
with tarfile.open(sdist) as archive:
    prefix = "fin_harness-0.1.0/"
    names = archive.getnames()
    for directory in ("protocol", "tests/fixtures", "integrations", "docs"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file():
                assert prefix + path.relative_to(ROOT).as_posix() in names, path
    source = json.load(archive.extractfile(prefix + "tests/fixtures/source-success.json"))
    request = json.load(archive.extractfile(prefix + "protocol/v1/fixtures/analyze-success.request.json"))

with tempfile.TemporaryDirectory(prefix="fin-wheel-check-") as temporary:
    environment = Path(temporary) / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, timeout=30)
    python = environment / "bin/python"
    subprocess.run(["uv", "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel)],
                   check=True, timeout=30)
    smoke = """
import json, sys
from pathlib import Path
import fin_harness
from fin_harness.protocol import load_schema
from fin_harness.core import Engine
from fin_harness.store import Store
assert Path(fin_harness.__file__).is_relative_to(Path(sys.prefix))
for name in ('request', 'analyze-response', 'explain-response'):
    assert load_schema(name)['type'] == 'object'
inputs = json.load(sys.stdin)
with Store(':memory:') as store:
    store.import_document(inputs['source'])
    engine = Engine(store)
    result = engine.handle(inputs['request'])
    assert result['results'][0]['value'] == '0.3333'
    assert engine.replay(result['run_id'])['match']
print('Clean wheel: installed schemas, analyze and replay passed without runtime dependencies')
"""
    subprocess.run([str(python), "-I", "-c", smoke], input=json.dumps({"source": source, "request": request}),
                   text=True, cwd=temporary, check=True, timeout=10)
print("Distribution contents passed")
