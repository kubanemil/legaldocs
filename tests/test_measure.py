import shutil
from pathlib import Path

import pytest

from nsm.measure import ENCLAVE_DIRS, measure

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for directory in ENCLAVE_DIRS:
        shutil.copytree(ROOT / directory, tmp_path / directory)
    return tmp_path


def test_pcrs_are_reproducible_from_any_checkout(tree: Path) -> None:
    assert measure(tree) == measure(ROOT)


def test_changing_enclave_code_changes_pcr0_and_pcr2_only(tree: Path) -> None:
    before = measure(tree)
    extractor = tree / "enclave" / "extractor.py"
    extractor.write_text(extractor.read_text() + "\nLEAK = True\n")
    after = measure(tree)
    assert after[0] != before[0]
    assert after[2] != before[2]
    assert after[1] == before[1]


def test_changing_locked_dependencies_changes_pcr0_and_pcr1(tree: Path) -> None:
    before = measure(tree)
    requirements = tree / "enclave" / "requirements.txt"
    requirements.write_text(requirements.read_text() + "evil==1.0\n")
    after = measure(tree)
    assert (after[0], after[1]) != (before[0], before[1])
    assert after[2] == before[2]


def test_bytecode_caches_and_dotfiles_are_not_measured(tree: Path) -> None:
    before = measure(tree)
    (tree / "enclave" / "__pycache__").mkdir(exist_ok=True)
    (tree / "enclave" / "__pycache__" / "server.cpython-313.pyc").write_bytes(b"\0")
    (tree / "protocol" / ".server.py.swp").write_bytes(b"\0")
    assert measure(tree) == before


def test_a_tree_without_the_enclave_build_inputs_cannot_be_measured(tmp_path: Path) -> None:
    (tmp_path / "enclave").mkdir()
    with pytest.raises(FileNotFoundError):
        measure(tmp_path)
