import pytest

from yourtool.utils.formatting import format_size


@pytest.mark.parametrize(
    "num_bytes, expected",
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024 - 1, "1024.0 KB"),  # just under a megabyte: still KB
        (1024 * 1024, "1.0 MB"),
        (5 * 1024 * 1024, "5.0 MB"),
        (1024**3, "1.0 GB"),
        (3 * 1024**4, "3072.0 GB"),  # no TB unit: stays in GB
    ],
)
def test_format_size(num_bytes, expected):
    assert format_size(num_bytes) == expected
