import os
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from tools.github_api import get_user_repositories


def test_github_api_errors():
    async def run_checks():
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            with patch("tools.github_api.load_dotenv"):
                try:
                    await get_user_repositories()
                    raise AssertionError("Should have raised ValueError for missing token")
                except ValueError:
                    pass

        with patch.dict(os.environ, {"GITHUB_TOKEN": "fake_token"}):
            with patch("tools.github_api.load_dotenv"):
                mock_response = AsyncMock()
                mock_response.status_code = 401
                mock_response.text = "Unauthorized"

                with patch("httpx.AsyncClient.get", return_value=mock_response):
                    try:
                        await get_user_repositories()
                        raise AssertionError("Should have raised RuntimeError for API failure")
                    except RuntimeError:
                        pass

    asyncio.run(run_checks())


def test_uses_explicit_github_token_from_request():
    async def run_check():
        with patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=False):
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [{
                "id": 1,
                "name": "demo-repo",
                "full_name": "octocat/demo-repo",
                "private": False,
                "html_url": "https://github.com/octocat/demo-repo",
                "description": "demo",
                "language": "Python",
                "stargazers_count": 42,
                "updated_at": "2024-01-01T00:00:00Z",
            }]

            async def fake_get(*args, **kwargs):
                return mock_response

            with patch("httpx.AsyncClient.get", side_effect=fake_get) as mock_get:
                repos = await get_user_repositories("oauth-token")

            assert repos[0]["full_name"] == "octocat/demo-repo"
            assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer oauth-token"

    asyncio.run(run_check())
