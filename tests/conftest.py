import json
import os

import pytest
from infrahub_sdk.client import InfrahubClient


@pytest.fixture
def add_mock_response():
    def _add_mock_response(httpx_mock, mockname, method, url, key=None, **kwargs):
        mocks_dir = os.path.join(os.path.dirname(__file__), "mocks")
        with open(os.path.join(mocks_dir, mockname)) as f:
            data = json.load(f)
        if key:
            data = data[key]
        httpx_mock.add_response(method=method, url=url, json=data, **kwargs)

    return _add_mock_response


@pytest.fixture
def client() -> InfrahubClient:
    return InfrahubClient(address="http://localhost:8000")
