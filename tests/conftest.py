import pytest

from bankai.common import clock
from bankai.testing import World


@pytest.fixture(autouse=True)
def _reset_clock():
    clock.reset()
    yield
    clock.reset()


@pytest.fixture
async def world():
    w = await World.create()
    yield w
    await w.close()


@pytest.fixture
async def gullible_world():
    """The in-bank model is fully compromised: it obeys any injected instruction it reads."""
    w = await World.create(gullible=True)
    yield w
    await w.close()
