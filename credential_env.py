"""Shared, non-interpolating dotenv semantics for editing and startup."""
import io

KEY = 'MODEL_API_KEY'


class UnsupportedEnvFile(ValueError):
    """File cannot be edited without disagreeing with a credential reader."""


def bindings(raw, host_parser=None):
    from dotenv.parser import parse_stream
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise UnsupportedEnvFile('Unsupported credential encoding') from None
    if '\ufeff' in text:
        raise UnsupportedEnvFile('Unsupported credential BOM')
    parsed = list(parse_stream(io.StringIO(text)))
    for binding in parsed:
        # The host is line-oriented, while dotenv accepts multiline strings.
        # Refuse even unrelated multiline values: an embedded assignment could
        # become effective when a later target binding is removed.
        if binding.error or len(binding.original.string.strip().splitlines()) > 1:
            raise UnsupportedEnvFile('Unsupported credential syntax')
        if host_parser is not None:
            expected = {binding.key: binding.value} if binding.key is not None else {}
            # Compare EACH binding, not just the final mapping: duplicate keys
            # must not conceal a parser mismatch until removal reveals it.
            if host_parser(binding.original.string) != expected:
                raise UnsupportedEnvFile('Ambiguous credential syntax')
    return parsed


def saved(parsed):
    values = [binding.value or '' for binding in parsed if binding.key == KEY]
    return values[-1] if values else ''
