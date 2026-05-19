import pytest

from franc.naming import NamingConvention


def test_validate_valid_hostname():
    convention = NamingConvention(
        country_codes=["DE", "CH"],
        site_codes=["FRA1", "ZRH2"],
        device_codes=["SWI", "FWG"],
    )

    result = convention.validate("DE-FRA1-SWI-DCLAN-07")

    assert result.is_valid is True
    assert result.parts is not None
    assert result.parts.country_code == "DE"
    assert result.parts.site_code == "FRA1"
    assert result.parts.device_code == "SWI"
    assert result.parts.variable == "DCLAN"
    assert result.parts.sequence == 7


def test_validate_rejects_bad_pattern():
    convention = NamingConvention()

    result = convention.validate("de-fra1-swi-dclan-07")

    assert result.is_valid is False
    assert "hostname does not match required pattern" in result.errors


def test_generate_builds_and_validates():
    convention = NamingConvention(
        country_codes=["DE"],
        site_codes=["FRA1"],
        device_codes=["SWI"],
    )

    hostname = convention.generate(
        country_code="de",
        site_code="fra1",
        device_code="swi",
        variable="dclan",
        sequence=7,
    )

    assert hostname == "DE-FRA1-SWI-DCLAN-07"


def test_generate_rejects_sequence_out_of_range():
    convention = NamingConvention()

    with pytest.raises(ValueError, match="sequence must be an int between 1 and 99"):
        convention.generate(
            country_code="DE",
            site_code="FRA1",
            device_code="SWI",
            variable="DCLAN",
            sequence=0,
        )


def test_parse_raises_with_details():
    convention = NamingConvention(country_codes=["DE"])

    with pytest.raises(ValueError, match="unknown country code"):
        convention.parse("CH-ZRH2-FWG-SECURITY-02")
