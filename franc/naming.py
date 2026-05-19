from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_HOSTNAME_REGEX = r"^[A-Z]{2}-[A-Z0-9]{4}-[A-Z]{3}-[A-Z0-9-]{1,50}-\d{2}$"


@dataclass(frozen=True)
class NamingParts:
    country_code: str
    site_code: str
    device_code: str
    variable: str
    sequence: int


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    errors: tuple[str, ...]
    parts: NamingParts | None = None


class NamingConvention:
    def __init__(
        self,
        *,
        country_codes: Iterable[str] | None = None,
        site_codes: Iterable[str] | None = None,
        device_codes: Iterable[str] | None = None,
        max_length: int = 64,
        hostname_regex: str = DEFAULT_HOSTNAME_REGEX,
    ) -> None:
        self._country_codes = _to_upper_set(country_codes)
        self._site_codes = _to_upper_set(site_codes)
        self._device_codes = _to_upper_set(device_codes)
        self._max_length = max_length
        self._regex = re.compile(hostname_regex)

    def validate(self, hostname: str) -> ValidationResult:
        errors: list[str] = []

        if not isinstance(hostname, str) or not hostname:
            return ValidationResult(is_valid=False, errors=("hostname must be a non-empty string",))

        if len(hostname) > self._max_length:
            errors.append(f"hostname exceeds max length {self._max_length}")

        if not self._regex.match(hostname):
            errors.append("hostname does not match required pattern")

        parts = _split_parts(hostname)
        if parts is None:
            errors.append("hostname must have 5 segments separated by hyphens")
            return ValidationResult(is_valid=False, errors=tuple(errors))

        country_code, site_code, device_code, variable, sequence_str = parts

        if self._country_codes and country_code not in self._country_codes:
            errors.append(f"unknown country code: {country_code}")

        if self._site_codes and site_code not in self._site_codes:
            errors.append(f"unknown site code: {site_code}")

        if self._device_codes and device_code not in self._device_codes:
            errors.append(f"unknown device code: {device_code}")

        sequence = _parse_sequence(sequence_str)
        if sequence is None:
            errors.append("sequence must be 01-99")

        if errors:
            return ValidationResult(is_valid=False, errors=tuple(errors))

        assert sequence is not None  # guarded above: sequence is None adds to errors → early return
        return ValidationResult(
            is_valid=True,
            errors=(),
            parts=NamingParts(
                country_code=country_code,
                site_code=site_code,
                device_code=device_code,
                variable=variable,
                sequence=sequence,
            ),
        )

    def parse(self, hostname: str) -> NamingParts:
        result = self.validate(hostname)
        if not result.is_valid or result.parts is None:
            details = "; ".join(result.errors) if result.errors else "invalid hostname"
            raise ValueError(details)
        return result.parts

    def generate(
        self,
        *,
        country_code: str,
        site_code: str,
        device_code: str,
        variable: str,
        sequence: int,
    ) -> str:
        cc = country_code.upper()
        llll = site_code.upper()
        ttt = device_code.upper()
        var = variable.upper()
        seq = _format_sequence(sequence)

        hostname = f"{cc}-{llll}-{ttt}-{var}-{seq}"
        result = self.validate(hostname)
        if not result.is_valid:
            details = "; ".join(result.errors) if result.errors else "invalid hostname"
            raise ValueError(details)
        return hostname


def _to_upper_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {value.upper() for value in values}


def _split_parts(hostname: str) -> tuple[str, str, str, str, str] | None:
    parts = hostname.split("-")
    if len(parts) < 5:
        return None

    country_code = parts[0]
    site_code = parts[1]
    device_code = parts[2]
    sequence = parts[-1]
    variable = "-".join(parts[3:-1])

    if not variable:
        return None

    return country_code, site_code, device_code, variable, sequence


def _parse_sequence(sequence: str) -> int | None:
    if not sequence.isdigit() or len(sequence) != 2:
        return None
    value = int(sequence)
    if not 1 <= value <= 99:
        return None
    return value


def _format_sequence(sequence: int) -> str:
    if not isinstance(sequence, int) or not 1 <= sequence <= 99:
        raise ValueError("sequence must be an int between 1 and 99")
    return f"{sequence:02d}"
