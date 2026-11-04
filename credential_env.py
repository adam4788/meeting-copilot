"""Shared, non-interpolating dotenv semantics for editing and startup."""
import io

KEY = 'MODEL_API_KEY'


def bindings(raw):
    from dotenv.parser import parse_stream
    parsed = list(parse_stream(io.StringIO(raw.decode('utf-8'))))
    if any(binding.error for binding in parsed):
        raise ValueError('Malformed credential file')
    return parsed


def saved(parsed):
    values = [binding.value or '' for binding in parsed if binding.key == KEY]
    return values[-1] if values else ''
