import re

from tools.git_ops import normalize_repo_url


def test_normalize_repo_url_from_browser_url():
    url = "https://github.com/siddhipatil1885/test/tree/main"
    assert normalize_repo_url(url) == "https://github.com/siddhipatil1885/test.git"


def test_normalize_repo_url_with_repo_suffix_and_git():
    assert normalize_repo_url("https://github.com/octocat/hello-world.git") == "https://github.com/octocat/hello-world.git"


def test_normalize_repo_url_with_trailing_slash():
    assert normalize_repo_url("https://github.com/octocat/hello-world/") == "https://github.com/octocat/hello-world.git"
