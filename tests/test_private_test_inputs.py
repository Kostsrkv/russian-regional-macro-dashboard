import pytest

from conftest import missing_private_inputs


def test_available_input_is_not_skipped_even_if_its_content_is_invalid(tmp_path):
    path = tmp_path / "outputs/fixture.csv"
    path.parent.mkdir()
    path.write_text("Invalid analytical data")
    assert missing_private_inputs(tmp_path, ["outputs/fixture.csv"]) == []


def test_missing_input_is_explicit_not_substituted(tmp_path):
    assert missing_private_inputs(tmp_path, ["outputs/missing.csv"]) == ["outputs/missing.csv"]
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("path", ["../outside.csv", "/absolute.csv", ""])
def test_unbounded_input_paths_are_rejected(tmp_path, path):
    with pytest.raises(ValueError, match="project-relative"):
        missing_private_inputs(tmp_path, [path])
