import pytest
from app.services.geocoder import resolve_provider
from app.services.providers.axenta import AxentaProvider
from app.services.providers.gs_baltgps import GsBaltgpsProvider
from app.services.providers.maps_baltgps import MapsBaltgpsProvider


@pytest.mark.parametrize("url,cls", [
    ("https://axenta.cloud/locator/abc", AxentaProvider),
    ("https://gs.baltgps.ru/locator?authId=1", GsBaltgpsProvider),
    ("https://maps.baltgps.ru/locator/index.html?t=abc", MapsBaltgpsProvider),
])
def test_resolve(url, cls):
    assert isinstance(resolve_provider(url), cls)


def test_unknown():
    assert resolve_provider("https://example.com") is None
