"""Memory-only mutation evidence for EOF, startup and Windows test gates."""
import inspect
import io
import sys
import textwrap
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from overlay_bridge import OverlayBridge
from server import MeetingState
from test_overlay_descendants import OverlayDescendantTests
from test_overlay_portability import OverlayPortabilityTests
from test_overlay_reader_eof import ReaderEOFTests
from test_overlay_startup_admission import StartupAdmissionTests


def mutate(owner, name, replacements):
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    for old, new in replacements:
        assert old in source, f'missing mutation: {old}'
        source = source.replace(old, new, 1)
    namespace = dict(original.__globals__)
    exec(compile(source, '<eof-mutation>', 'exec'), namespace)
    return patch.object(owner, name, namespace[name])


def verify(label, contexts, case):
    output = io.StringIO()
    with ExitStack() as stack:
        for context in contexts:
            stack.enter_context(context)
        result = unittest.TextTestRunner(stream=output).run(case)
    if result.wasSuccessful() or result.errors or result.skipped:
        raise AssertionError(f'{label}: expected assertion failure\n{output.getvalue()}')
    print(f'{label}: RED confirmed')


def main():
    verify('reader completion is not EOF', [mutate(OverlayBridge, '_reap_locked', [
        ('pipe_eof = eof is None or eof.is_set()', 'pipe_eof = True')])],
        ReaderEOFTests('test_reader_io_error_is_not_eof_even_after_thread_exits'))
    verify('retired reader drains to actual EOF', [mutate(OverlayBridge, '_read_clicks', [
        ('continue  # drain without publishing; only actual EOF is evidence', 'break')])],
        ReaderEOFTests('test_retired_reader_records_only_its_own_real_eof'))
    verify('POSIX tests require explicit Windows skips', [
        patch.object(unittest, 'skipUnless', lambda *args: lambda obj: obj)],
        OverlayPortabilityTests('test_posix_pipe_tests_are_explicitly_skipped_on_simulated_windows'))
    if sys.platform in ('darwin', 'linux'):
        verify('original retired-line false confirmation', [
            mutate(OverlayBridge, '_read_clicks', [
                ('continue  # drain without publishing; only actual EOF is evidence', 'break')]),
            mutate(OverlayBridge, '_reap_locked', [
                ('pipe_eof = eof is None or eof.is_set()', 'pipe_eof = True')])],
            OverlayDescendantTests('test_retired_line_is_not_eof_from_escaped_stdout_holder'))
        verify('already dead helper cannot commit Show', [mutate(MeetingState, 'overlay_show', [
            ('and alive is True', 'and True'),
            ("and not isinstance(getattr(process, 'returncode', None), int)", 'and True')])],
            StartupAdmissionTests('test_show_rejects_helper_exited_before_startup_commit'))
        verify('direct Show rejects cached dead launch', [mutate(OverlayBridge, 'show_overlay', [
            ("if self.stopping or isinstance(getattr(process, 'returncode', None), int):", 'if False:')])],
            StartupAdmissionTests('test_direct_show_does_not_report_cached_dead_launch'))
        verify('reused Show rejects cached exit', [mutate(MeetingState, '_overlay_show', [
            ("if isinstance(getattr(process, 'returncode', None), int):", 'if False:')])],
            StartupAdmissionTests('test_reused_show_rejects_observed_exit_during_scope_update'))
        verify('startup poll must remain bounded', [mutate(MeetingState, 'overlay_show', [
            ('alive = self._overlay_process_alive(process) if process is not None else True',
             'alive = process.poll() is None if process is not None else True')])],
            StartupAdmissionTests('test_startup_liveness_probe_is_bounded_and_off_lifecycle_lock'))
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (ReaderEOFTests, OverlayPortabilityTests,
                                           OverlayDescendantTests, StartupAdmissionTests))
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        raise SystemExit(1)
    print('Restored production: GREEN')


if __name__ == '__main__':
    main()
