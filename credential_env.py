"""Shared, non-interpolating dotenv semantics for editing and startup."""
import io

KEY = 'MODEL_API_KEY'


TOKENIZER_UNAVAILABLE = ('credential_tokenizer_unavailable: Key settings unavailable because the Hermes host tokenizer is unavailable. '
                         'The saved file is not loaded by the startup bridge. Upgrade Hermes, then safely restart after active sessions finish; '
                         'or have your administrator supply MODEL_API_KEY through the service environment. Never paste keys in chat.')


class HostTokenizerUnavailable(RuntimeError):
    pass


def require_host_parser(scope):
    parser = getattr(scope, '_parse_env_text', None)
    if not callable(parser):
        raise HostTokenizerUnavailable(TOKENIZER_UNAVAILABLE)
    return parser


class UnsupportedEnvFile(ValueError):
    """File cannot be edited without disagreeing with a credential reader."""


def _text(raw):
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise UnsupportedEnvFile('Unsupported credential encoding') from None
    if '\ufeff' in text:
        raise UnsupportedEnvFile('Unsupported credential BOM')
    return text


def startup_value(raw, host_parser):
    """Compare all target occurrences, including shadowed ones, on exact bytes.

    Hermes tokenizes physical lines; dotenv tokenizes whole bindings. Keep each
    target's source offset AND unexpanded value, not only the last-value map.
    Unrelated differences are safe for reading, never for the editor's rewrites.
    No copied host tokenizer or fallback is trusted here.
    """
    from dotenv.parser import parse_stream
    text = _text(raw)
    dotenv_targets, host_targets = [], []
    offset = 0
    for binding in parse_stream(io.StringIO(text)):
        source = binding.original.string
        if binding.error:
            raise UnsupportedEnvFile('Unsupported credential syntax')
        if binding.key == KEY:
            dotenv_targets.append((offset + len(source) - len(source.lstrip()), binding.value))
        offset += len(source)
    offset = 0
    for line in text.splitlines(keepends=True):
        parsed = host_parser(line)
        if KEY in parsed:
            host_targets.append((offset + len(line) - len(line.lstrip()), parsed[KEY]))
        offset += len(line)
    if dotenv_targets != host_targets:
        raise UnsupportedEnvFile('Ambiguous target credential syntax')
    expected = {KEY: dotenv_targets[-1][1]} if dotenv_targets else {}
    # Also verify the full-file host result, to refuse context-sensitive drift.
    actual = host_parser(text)
    if {key: value for key, value in actual.items() if key == KEY} != expected:
        raise UnsupportedEnvFile('Ambiguous target credential syntax')
    return expected.get(KEY) or ''


def bindings(raw, host_parser=None):
    from dotenv.parser import parse_stream
    text = _text(raw)
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
