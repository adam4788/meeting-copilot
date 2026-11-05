"""Device-free native writer probe. Compiles only Foundation and production emit.

No Capture class, audio frameworks, permission requests, or actual audio input.
Reports synthetic scheduling experiments, not hardware reliability evidence.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import time

DRIVER = r'''
@main struct SyntheticWriter {
    static func main() {
        let stall = Double(CommandLine.arguments[1])!
        if stall > 0 { writer.async { Thread.sleep(forTimeInterval: stall) } }
        let samples = 171
        let duration = 1.8
        let start = ProcessInfo.processInfo.systemUptime
        let pcm = Data(repeating: 0, count: samples * 4).base64EncodedString()
        var attempted = 0
        while ProcessInfo.processInfo.systemUptime - start < duration {
            emit(["type":"pcm", "source":"microphone", "rate":16000,
                  "samples":samples, "timestamp":Date().timeIntervalSince1970, "pcm":pcm])
            attempted += 1
            let remaining = start + Double(attempted * samples) / 16000 - ProcessInfo.processInfo.systemUptime
            if remaining > 0 { Thread.sleep(forTimeInterval: remaining) }
        }
        writer.sync {}
        let pending = dropCounter.sync { droppedFrames }
        let metadata: [String: Any] = ["attempted": attempted, "pending": pending,
                                      "timing_ms": writer.sync { writerTiming() }]
        FileHandle.standardError.write(try! JSONSerialization.data(withJSONObject: metadata))
    }
}
'''


def writer_source(root):
    native = (Path(root) / 'native/macos_capture.swift').read_text()
    # Extract the real bounded queue and serialization/write/loss code. A changed
    # production boundary fails loudly rather than silently testing an old copy.
    start = native.index('private let writer =')
    end = native.index('private var terminating =')
    return 'import Foundation\n' + native[start:end] + DRIVER


def measure(binary, writer_stall=0, reader_stall=0):
    proc = subprocess.Popen([str(binary), str(writer_stall)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(reader_stall)
        output, metadata = proc.communicate(timeout=10)
        if proc.returncode:
            raise RuntimeError(f'Synthetic writer failed ({proc.returncode})')
        rows = [json.loads(line) for line in output.splitlines()]
        counts = json.loads(metadata)
        received = len(rows)
        reported = sum(row.get('native_dropped_chunks', 0) for row in rows)
        assert received + reported + counts['pending'] == counts['attempted'], counts
        assert all(row['samples'] == 171 and row['type'] == 'pcm' for row in rows)
        return dict(writer_stall_ms=writer_stall * 1000,
                    reader_stall_ms=reader_stall * 1000,
                    received=received, reported_drops=reported, **counts)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def main():
    with tempfile.TemporaryDirectory(prefix='native-backpressure-') as tmp:
        root = Path(tmp)
        source = root / 'Writer.swift'
        source.write_text(writer_source(Path(__file__).parent))
        binary = root / 'writer'
        build = subprocess.run(['swiftc', '-parse-as-library', '-module-cache-path',
                                str(root / 'module-cache'), str(source), '-o', str(binary)],
                               capture_output=True, text=True, timeout=120)
        if build.returncode:
            raise RuntimeError(f'Synthetic writer compile failed ({build.returncode}):\n'
                               + build.stderr[:12000])
        results = [measure(binary), measure(binary, writer_stall=.1),
                   measure(binary, writer_stall=.2), measure(binary, reader_stall=.5),
                   measure(binary, reader_stall=1.5)]
        # A forced scheduling pause must exercise the loss path. Exact counts
        # depend on OS scheduling and are deliberately not asserted.
        assert results[2]['reported_drops'] + results[2]['pending'] > 0
        assert results[2]['timing_ms']['max_queue_ms'] >= 100
        print(json.dumps({'physical_capture': False, 'results': results}, indent=2))


if __name__ == '__main__':
    main()
