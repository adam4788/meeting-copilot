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
from test_overlay_eof_recovery import EOFRecoveryTests
from test_overlay_channel_health import ChannelHealthTests
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
        ("eof is not None and eof.is_set()", "True")])],
        ReaderEOFTests('test_reader_io_error_is_not_eof_even_after_thread_exits'))
    verify('retired reader drains to actual EOF', [mutate(OverlayBridge, '_read_clicks', [
        ('continue  # drain without publishing; only actual EOF is evidence', 'break')])],
        ReaderEOFTests('test_retired_reader_records_only_its_own_real_eof'))
    verify('POSIX tests require explicit Windows skips', [
        patch.object(unittest, 'skipUnless', lambda *args: lambda obj: obj)],
        OverlayPortabilityTests('test_posix_pipe_tests_are_explicitly_skipped_on_simulated_windows'))
    verify('no concurrent recovery reader', [mutate(OverlayBridge, '_recover_stdout_eof', [
        ('or (reader is not None and reader.is_alive())', 'or False')])],
        ReaderEOFTests('test_recovery_never_reads_concurrent_or_replaced_pipe'))
    verify('recovery owns the original pipe', [mutate(OverlayBridge, '_recover_stdout_eof', [
        ("or stream is not getattr(target, 'stdout', None)", 'or False')])],
        ReaderEOFTests('test_recovery_never_reads_concurrent_or_replaced_pipe'))
    verify('missing pipe evidence fails closed', [mutate(OverlayBridge, '_reap_locked', [
        ("or (eof is not None and eof.is_set()))", "or eof is None or eof.is_set())")])],
        ReaderEOFTests('test_missing_pipe_evidence_cannot_confirm'))
    if sys.platform in ('darwin', 'linux'):
        verify('protocol text-mode decode must not lose EOF', [mutate(OverlayBridge, '_check_helper_protocol', [
            ('text=False', 'text=True'),
            ("output = output.decode('utf-8', errors='strict')", 'pass')])],
            ChannelHealthTests('test_malformed_protocol_exit_zero_allows_hide_and_new_start'))
        verify('protocol EOF recorded before strict decoding', [mutate(OverlayBridge, '_check_helper_protocol', [
            ("self._stdout_eof[target].set()\n            output = output.decode('utf-8', errors='strict')",
             "output = output.decode('utf-8', errors='strict')\n            self._stdout_eof[target].set()")])],
            ChannelHealthTests('test_malformed_protocol_exit_zero_allows_hide_and_new_start'))
        verify('protocol decoding stays strict', [mutate(OverlayBridge, '_check_helper_protocol', [
            ("errors='strict'", "errors='ignore'")])],
            ChannelHealthTests('test_protocol_decode_is_strict_even_around_valid_token'))
        verify('protocol arbitrary exception/closed handle is not EOF', [mutate(OverlayBridge, '_check_helper_protocol', [
            ('finally:\n            with self.lock:',
             'finally:\n            self._stdout_eof[target].set()\n            with self.lock:')])],
            ChannelHealthTests('test_protocol_read_exception_and_closed_handle_are_not_eof'))
        verify('protocol live holder cannot invent EOF', [mutate(OverlayBridge, '_recover_stdout_eof', [
            ('pass  # includes EAGAIN: a live holder is not EOF', 'eof.set()')])],
            ChannelHealthTests('test_protocol_live_pipe_holder_retains_uncertainty_until_real_eof'))
        for entry in ('fresh_direct', 'fresh_controller', 'repeated_direct', 'repeated_controller'):
            verify(f'active stdout EOF rejects {entry} Show', [mutate(OverlayBridge, '_read_clicks', [
                ('self._reader_failed.add(process)', 'pass  # lost active EOF health')])],
                ChannelHealthTests(f'test_{entry}_show_rejects_active_stdout_eof'))
        verify('reader failure recovers real EOF', [
            patch.object(OverlayBridge, '_recover_stdout_eof', lambda *args: None)],
            EOFRecoveryTests('test_decode_failure_recovers_only_after_actual_eof'))
        verify('recovery cannot invent EOF for a live holder', [mutate(OverlayBridge, '_recover_stdout_eof', [
            ("pass  # includes EAGAIN: a live holder is not EOF", "eof.set()")])],
            EOFRecoveryTests('test_decode_failure_recovers_only_after_actual_eof'))
        verify('cancelled startup registers evidence at publication', [mutate(OverlayBridge, 'start_helper', [
            ('self._register_stdout_locked(target)', 'pass  # lost obligation')]),
            patch.object(OverlayBridge, '_start_stdout_reader', lambda *args: None)],
            EOFRecoveryTests('test_retired_startup_registers_pipe_before_publication'))
        verify('direct Show rejects known reader failure', [mutate(OverlayBridge, 'show_overlay', [
            ('if self.process in self._reader_failed:', 'if False:')])],
            EOFRecoveryTests('test_known_reader_failure_rejects_direct_and_controller_show'))
        verify('controller startup rejects known reader failure', [mutate(MeetingState, 'overlay_show', [
            ("and process not in getattr(bridge, '_reader_failed', ())", 'and True')])],
            EOFRecoveryTests('test_controller_startup_commit_rejects_known_reader_failure'))
        verify('controller reuse rejects known reader failure', [
            mutate(MeetingState, '_reconcile_overlay_admission', [
                ("or process in getattr(bridge, '_reader_failed', ())", 'or False')]),
            mutate(MeetingState, '_overlay_show', [
                ("or process in getattr(self.overlay, '_reader_failed', ())", 'or False')])],
            EOFRecoveryTests('test_known_reader_failure_rejects_direct_and_controller_show'))
        verify('original retired-line false confirmation', [
            mutate(OverlayBridge, '_read_clicks', [
                ('continue  # drain without publishing; only actual EOF is evidence', 'break')]),
            mutate(OverlayBridge, '_reap_locked', [
                ("eof is not None and eof.is_set()", "True")])],
            OverlayDescendantTests('test_retired_line_is_not_eof_from_escaped_stdout_holder'))
        # An exited child's EOF now supplies independent unhealthy evidence;
        # remove both guards to recreate the original dead-Show acceptance.
        verify('already dead helper cannot commit Show', [mutate(MeetingState, 'overlay_show', [
            ('and alive is True', 'and True'),
            ("and process not in getattr(bridge, '_reader_failed', ())", 'and True'),
            ("and not isinstance(getattr(process, 'returncode', None), int)", 'and True')])],
            StartupAdmissionTests('test_show_rejects_helper_exited_before_startup_commit'))
        verify('direct Show rejects cached dead launch', [mutate(OverlayBridge, 'show_overlay', [
            ('if process in self._reader_failed:', 'if False:'),
            ("if self.stopping or isinstance(getattr(process, 'returncode', None), int):", 'if False:')])],
            StartupAdmissionTests('test_direct_show_does_not_report_cached_dead_launch'))
        verify('reused Show rejects cached exit', [mutate(MeetingState, '_overlay_show', [
            ("isinstance(getattr(process, 'returncode', None), int)", "False"),
            ("process in getattr(self.overlay, '_reader_failed', ())", "False")])],
            StartupAdmissionTests('test_reused_show_rejects_observed_exit_during_scope_update'))
        verify('startup poll must remain bounded', [mutate(MeetingState, 'overlay_show', [
            ('alive = self._overlay_process_alive(process) if process is not None else True',
             'alive = process.poll() is None if process is not None else True')])],
            StartupAdmissionTests('test_startup_liveness_probe_is_bounded_and_off_lifecycle_lock'))
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (ReaderEOFTests, EOFRecoveryTests, ChannelHealthTests,
                                           OverlayPortabilityTests, OverlayDescendantTests, StartupAdmissionTests))
    if not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful():
        raise SystemExit(1)
    print('Restored production: GREEN')


if __name__ == '__main__':
    main()
