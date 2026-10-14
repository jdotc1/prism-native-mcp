"""Direct local Apple Events and Prism script execution, with operation evidence.

No dependency on agent-conversation tools. Prism itself imports, calculates,
duplicates native families, formats, saves and exports the scientific objects.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time
import uuid
from urllib.parse import unquote, urlparse

from .errors import PrismError

APP = Path('/Applications/Prism 11.app')
BUNDLE = 'com.GraphPad.Prism'
VERIFIED_VERSION = ('11.1.0', '110218')
CONTROL_ROOT = Path.home() / 'Library/Application Support/PrismMCP-Control'
RECOVERY_EVIDENCE_LIMIT = 16 * 1024 * 1024
PLACEMENT_TRANSITION_SECONDS = 2.0


def apple_quote(value: str | Path) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    os.replace(temp, path)


def quote(value: str | Path) -> str:
    s = str(value)
    if any(c in s for c in ('"', '\n', '\r', '\x00')):
        raise PrismError('INVALID_ARGUMENT', 'Prism script argument contains unsupported characters.')
    return '"' + s.replace('%', '%%') + '"'


def decode_log(data: bytes) -> str:
    if not data:
        return ''
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        return data.decode('utf-32', errors='replace')
    if data[:4][1:] == b'\x00\x00\x00':
        return data[:len(data) - len(data) % 4].decode('utf-32-le', errors='replace')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16', errors='replace')
    return data.decode('utf-8', errors='replace')


def osa(source: str, *, jxa=False, timeout=8) -> str:
    command = ['/usr/bin/osascript'] + (['-l', 'JavaScript'] if jxa else [])
    try:
        p = subprocess.run(command, input=source, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PrismError('CONTROL_TIMEOUT', 'Apple Events control timed out.', timeout_seconds=timeout) from exc
    if p.returncode:
        raise PrismError('CONTROL_UNAVAILABLE', 'Apple Events or Accessibility control failed.', stderr=p.stderr[-2000:])
    return p.stdout.strip()


def application_info() -> dict:
    path = Path(os.environ.get('PRISM_MCP_APP', str(APP)))
    plist = path / 'Contents/Info.plist'
    if not plist.is_file():
        return {'installed': False, 'path': str(path), 'version_compatible': False}
    info = plistlib.loads(plist.read_bytes())
    version, build = info.get('CFBundleShortVersionString'), info.get('CFBundleVersion')
    return {'installed': True, 'path': str(path), 'bundle_id': info.get('CFBundleIdentifier'),
            'version': version, 'build': build, 'version_compatible': (version, build) == VERIFIED_VERSION}


def verify_running_application():
    value = osa('''tell application "System Events"
set ps to every process whose bundle identifier is "com.GraphPad.Prism"
if (count ps) is 0 then return ""
if (count ps) is not 1 then error "Multiple Prism processes"
return POSIX path of (application file of item 1 of ps as alias)
end tell''')
    if value and Path(value).resolve() != Path(application_info()['path']).resolve():
        raise PrismError('WRONG_APPLICATION', 'A different Prism installation is running.', running_path=value)
    return value


def windows() -> list[dict]:
    source = '''
var se = Application('System Events');
var ps = se.processes.whose({bundleIdentifier: 'com.GraphPad.Prism'})();
JSON.stringify(ps.length ? ps[0].windows().map(w => {
  var doc = null; try {doc = w.attributes.byName('AXDocument').value()} catch(e) {}
  var edited = null; try {var bs=w.menuButtons().filter(b=>b.description()==='document actions');
    if(bs.length===1) edited=bs[0].name()==='Edited';} catch(e) {}
  return {title:w.name(), document_url:doc, edited:edited, modal:w.attributes.byName('AXModal').value()};
}) : []);
'''
    rows = json.loads(osa(source, jxa=True))
    for row in rows:
        url = row.get('document_url')
        row['path'] = str(Path(unquote(urlparse(url).path)).resolve()) if url and url.startswith('file:') else None
    return rows


def native_script_windows() -> list[dict]:
    """Identify modal dialogs and native script/controller progress windows.

    Prism's incoming .pzc handler silently ignores scripts while a modal
    session exists. A completion log is written before controller cleanup.
    """
    import re
    return [window for window in windows()
            if window.get('modal') is True or window.get('title') == 'GraphPad Prism Script'
            or re.fullmatch(r'\d+% done', window.get('title') or '')]


def _recovery_bytes(path: Path, *, optional=False) -> tuple[bytes, dict] | None:
    """Read a bounded stable local evidence file without following a final symlink."""
    try:
        if path.is_symlink():
            raise PrismError('RECOVERY_REQUIRED', 'Recovery evidence cannot be a symbolic link.', path=str(path))
        if optional and not path.exists():
            return None
        before = path.stat()
        if not path.is_file() or before.st_size > RECOVERY_EVIDENCE_LIMIT:
            raise PrismError('RECOVERY_REQUIRED', 'Recovery evidence is not a bounded regular file.', path=str(path))
        data = path.read_bytes()
        after = path.stat()
        identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        if identity(before) != identity(after):
            raise PrismError('RECOVERY_REQUIRED', 'Recovery evidence changed while being read.', path=str(path))
        return data, {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
                      'bytes': len(data), 'mtime_ns': after.st_mtime_ns}
    except OSError as exc:
        raise PrismError('RECOVERY_REQUIRED', 'Required recovery evidence is unavailable.', path=str(path),
                         cause=type(exc).__name__) from exc


def _recovery_object(data: bytes, path: Path) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate key')
            value[key] = item
        return value

    try:
        value = json.loads(data, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError('expected object')
        return value
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise PrismError('RECOVERY_REQUIRED', 'Recovery checkpoint is corrupt or unrecognized; quarantine remains.',
                         path=str(path), cause=type(exc).__name__) from exc


class Operation:
    def __init__(self, state: Path, kind: str, inputs: dict, request_id: str | None = None):
        self.state = state
        self.run_id = uuid.uuid4().hex
        self.directory = state / 'runs' / self.run_id
        self.directory.mkdir(parents=True, mode=0o700)
        self.record = {'run_id': self.run_id, 'kind': kind, 'status': 'created',
                       'request_id': request_id, 'created_at': time.time(),
                       'input_fingerprint': hashlib.sha256(json.dumps(inputs, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                       'target_document': inputs.get('path'), 'steps': [], 'directory': str(self.directory)}
        self.persist()

    def persist(self):
        write_json(self.directory / 'operation.json', self.record)

    def finish(self, result: dict):
        self.record.update(status='succeeded', completed_at=time.time(), result=result)
        self.persist()

    def fail(self, exc: PrismError):
        self.record.update(status='failed', completed_at=time.time(), error=exc.as_dict())
        self.persist()

    def script(self, commands: list[str], *, expected: list[Path] = (), timeout=40) -> dict:
        if (self.directory / 'cancel.request').exists():
            raise PrismError('CANCELLED', 'Operation cancelled before native dispatch.', run_id=self.run_id)
        number = len(self.record['steps']) + 1
        stepdir = self.directory / f'step-{number:02d}'
        stepdir.mkdir()
        script = stepdir / 'run.pzc'
        log = script.with_suffix('.log')
        done = stepdir / 'completed.txt'
        token = f'{self.run_id}:{number}:{uuid.uuid4().hex}'
        version_file = stepdir / 'engine-version.txt'
        script.write_text('\n'.join(['CreateLog', f'OpenOutput {quote(version_file)}, clear', 'WVersion', 'CloseOutput',
                                     *commands, f'OpenOutput {quote(done)}, clear',
                                     f'WText {quote(token)}', 'CloseOutput', '']))
        step = {'number': number, 'script': str(script), 'script_sha256': digest(script),
                'log': str(log), 'completion_token': token, 'expected_outputs': [str(x) for x in expected],
                'status': 'running', 'started_at': time.time(), 'started_at_ns': time.time_ns(),
                'output_baselines': {str(p): {'mtime_ns': p.stat().st_mtime_ns, 'sha256': digest(p)} for p in expected if p.exists()}}
        self.record['status'] = 'running'
        self.record['steps'].append(step)
        self.persist()
        from .placement import enforce
        step['placement_events'] = []

        def placement_check(phase):
            event = {'phase': phase, 'checked_at': time.time()}
            try:
                result = enforce()
            except PrismError as exc:
                event.update(status='failed', error=exc.as_dict())
                step['placement_events'].append(event)
                raise
            event.update(status='succeeded', result=result)
            step['placement_events'].append(event)
            return result

        try:
            verify_running_application()
            pending_windows = native_script_windows()
            if pending_windows:
                raise PrismError('PRISM_BUSY', 'A native modal dialog or script controller is still open; no new script was dispatched.',
                                 native_script_windows=pending_windows)
            placement_check('before_dispatch')
        except PrismError as exc:
            step.update(status='failed', completed_at=time.time(), error=exc.as_dict())
            self.persist()
            raise
        CONTROL_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        inflight = CONTROL_ROOT / 'inflight.json'
        write_json(inflight, {'run_id': self.run_id, 'script': str(script), 'log': str(log),
                             'done': str(done), 'token': token, 'operation': str(self.directory / 'operation.json')})
        source = f'tell application {apple_quote(application_info()["path"])} to open POSIX file {apple_quote(script)}\n'
        process = subprocess.Popen(['/usr/bin/osascript', '-'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process.stdin.write(source)
        process.stdin.close()
        started = time.monotonic()
        logtext = ''
        next_placement_check = 0.0
        placement_transient_since = None
        native_terminal_verified = False
        cleanup_uncertain = False
        try:
            while time.monotonic() - started < timeout or step.get('native_completion_verified'):
                # Terminal evidence takes priority over a window disappearing
                # while Prism closes its script or welcome dialog.
                if log.exists():
                    logtext = decode_log(log.read_bytes())
                if logtext.rstrip().endswith('PROBLEM! Not completed because of error.'):
                    native_terminal_verified = True
                    cleanup_uncertain = True
                    step.update(native_terminal_outcome='failed', native_cleanup_uncertain=True)
                    self.persist()
                    self._dismiss_error(script)
                    try:
                        step['sender_returncode'] = process.wait(timeout=15)
                    except subprocess.TimeoutExpired as exc:
                        raise PrismError('PRISM_CLEANUP_TIMEOUT', 'Prism recorded a script error, but its original sender has not returned; quarantine remains.',
                                         run_id=self.run_id, log=str(log)) from exc
                    cleanup_deadline = time.monotonic() + 10
                    while native_script_windows():
                        if time.monotonic() >= cleanup_deadline:
                            raise PrismError('PRISM_CLEANUP_TIMEOUT', 'Prism recorded a script error, but its controller remains open; quarantine remains.',
                                             run_id=self.run_id, log=str(log))
                        time.sleep(.1)
                    cleanup_uncertain = False
                    step.update(native_cleanup_uncertain=False, native_control_ready=True,
                                native_control_ready_at=time.time())
                    raise PrismError('PRISM_SCRIPT_ERROR', 'Prism stopped the current script.',
                                     run_id=self.run_id, log=str(log), native_error=logtext[-2500:])
                complete = done.is_file() and token == decode_log(done.read_bytes()).strip()
                if complete and logtext.rstrip().endswith('COMPLETE! No Errors.'):
                    native_terminal_verified = True
                    if not step.get('native_completion_verified'):
                        cleanup_uncertain = True
                        step.update(native_completion_verified=True, native_terminal_outcome='completed',
                                    native_cleanup_uncertain=True,
                                    native_completion_verified_at=time.time(),
                                    completion_evidence={'done_sha256': digest(done), 'log_sha256': digest(log)})
                        self.persist()
                now = time.monotonic()
                if step.get('native_completion_verified') or now >= next_placement_check:
                    phase = 'after_native_completion' if step.get('native_completion_verified') else 'while_running'
                    try:
                        placement_check(phase)
                    except PrismError as exc:
                        if exc.code != 'PLACEMENT_TRANSIENT':
                            raise
                        if placement_transient_since is None:
                            placement_transient_since = now
                        if time.monotonic() - placement_transient_since >= PLACEMENT_TRANSITION_SECONDS:
                            raise PrismError('PLACEMENT_UNAVAILABLE', 'Prism windows did not stabilize for placement verification.',
                                             retry_seconds=PLACEMENT_TRANSITION_SECONDS,
                                             native_completion_verified=bool(step.get('native_completion_verified')),
                                             last_error=exc.as_dict()) from exc
                        next_placement_check = time.monotonic() + .1
                        time.sleep(.1)
                        continue
                    placement_transient_since = None
                    next_placement_check = time.monotonic() + .4
                if step.get('native_completion_verified'):
                    # The terminal log precedes native controller cleanup.
                    # Keep the original Apple Event sender alive until it has
                    # returned, then require the script/progress UI to be gone.
                    cleanup_uncertain = True
                    step['native_cleanup_uncertain'] = True
                    try:
                        sender_returncode = process.wait(timeout=15)
                    except subprocess.TimeoutExpired as exc:
                        raise PrismError('PRISM_CLEANUP_TIMEOUT', 'Prism wrote its terminal log but the original script sender has not returned; quarantine remains.',
                                         run_id=self.run_id) from exc
                    step['sender_returncode'] = sender_returncode
                    if sender_returncode != 0:
                        raise PrismError('PRISM_CLEANUP_FAILED', 'The native script sender did not finish successfully; quarantine remains.',
                                         run_id=self.run_id, sender_returncode=sender_returncode)
                    cleanup_deadline = time.monotonic() + 10
                    while native_script_windows():
                        if time.monotonic() >= cleanup_deadline:
                            raise PrismError('PRISM_CLEANUP_TIMEOUT', 'Prism wrote its terminal log but its script/progress controller remains open; quarantine remains.',
                                             run_id=self.run_id)
                        time.sleep(.1)
                    cleanup_uncertain = False
                    step.update(native_cleanup_uncertain=False, native_control_ready=True,
                                native_control_ready_at=time.time())
                    missing = [str(p) for p in expected if not p.is_file() or p.stat().st_size == 0]
                    if missing:
                        raise PrismError('MISSING_OUTPUT', 'Prism completed without every expected nonempty output.', outputs=missing)
                    stale = [str(p) for p in expected if str(p) in step['output_baselines'] and p.stat().st_mtime_ns <= step['output_baselines'][str(p)]['mtime_ns']]
                    if stale:
                        raise PrismError('STALE_OUTPUT', 'Expected output was not refreshed by the current operation.', outputs=stale)
                    verify_running_application()
                    step.update(status='succeeded', completed_at=time.time(),
                                native_version=decode_log(version_file.read_bytes()) if version_file.exists() else None,
                                output_sha256={str(p): digest(p) for p in expected})
                    (stepdir / 'run.log.txt').write_text(logtext)
                    self.persist()
                    inflight.unlink(missing_ok=True)
                    return step
                if process.poll() not in (None, 0):
                    raise PrismError('CONTROL_UNAVAILABLE', 'Prism did not accept the script Apple Event.',
                                     stderr=process.stderr.read()[-2000:], run_id=self.run_id)
                time.sleep(.1)
            # Killing osascript never means Prism stopped. A durable quarantine prevents races.
            raise PrismError('PRISM_TIMEOUT', 'Native completion is uncertain; mutations are quarantined until recovery.',
                             run_id=self.run_id, log=str(log), timeout_seconds=timeout)
        except PrismError as exc:
            step.update(status='failed', completed_at=time.time(), error=exc.as_dict(),
                        native_terminal_verified=native_terminal_verified)
            (stepdir / 'run.log.txt').write_text(logtext)
            self.persist()
            if native_terminal_verified and not cleanup_uncertain:
                inflight.unlink(missing_ok=True)
            raise
        finally:
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=1)
            for pipe in (process.stdout, process.stderr):
                pipe.close()

    @staticmethod
    def _dismiss_error(script: Path):
        # Exact native dialog title plus the current script's path, never a generic alert.
        try:
            osa('''var se=Application('System Events'); var p=se.processes.byName('Prism');
p.frontmost=true;delay(.15);
var ws=p.windows.whose({name:'GraphPad Prism Script'})();
function texts(e){var a=[];try{if(e.role()==='AXStaticText')a.push(e.value())}catch(_){};for(var c of e.uiElements())a=a.concat(texts(c));return a}
if(ws.length===1){var values=texts(ws[0]);
if(values.includes('ERROR! STOPPED!') && (values.includes(''' + json.dumps(str(script)) + ''') ||
values.includes(''' + json.dumps(str(script).replace(str(Path.home()), '~')) + '''))){
ws[0].buttons.byName('Close').actions.byName('AXPress').perform();
for(var i=0;i<60;i++){if(p.windows.whose({name:'GraphPad Prism Script'})().length===0)break;delay(.05)}
}}
''', jxa=True)
        except PrismError:
            pass


class Runtime:
    def __init__(self, state_dir: str | Path | None = None):
        self.state = Path(state_dir or os.environ.get('PRISM_MCP_STATE_DIR',
                          str(Path.home() / 'Library/Application Support/PrismMCP'))).resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        CONTROL_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def locked(self, *, recovery=False):
        with (CONTROL_ROOT / 'prism.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PrismError('PRISM_BUSY', 'Another native operation holds the Prism lock.') from exc
            try:
                if not recovery and (CONTROL_ROOT / 'inflight.json').exists():
                    try:
                        checkpoint = json.loads((CONTROL_ROOT / 'inflight.json').read_text())
                    except (OSError, ValueError):
                        checkpoint = {'path': str(CONTROL_ROOT / 'inflight.json'), 'readable': False}
                    raise PrismError('RECOVERY_REQUIRED', 'An earlier operation has uncertain completion.',
                                     checkpoint=checkpoint)
                app = application_info()
                if not app['installed']:
                    raise PrismError('APP_UNAVAILABLE', 'The configured Prism application is not installed.', application=app)
                if not app['version_compatible']:
                    raise PrismError('UNVERIFIED_VERSION', 'Native mutations are pinned to Prism 11.1.0 build 110218.', application=app)
                verify_running_application()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def operation(self, kind: str, inputs: dict, request_id: str | None = None) -> Operation:
        return Operation(self.state, kind, inputs, request_id)

    def recover(self) -> dict:
        """Clear uncertainty only after verifying this exact native script terminated.

        This does not publish checkpoints, retry commands, or change an operation
        into a successful scientific result. Process absence alone cannot exclude
        an Apple Event still in transit and therefore never clears quarantine.
        """
        recovery_id = uuid.uuid4().hex
        record_path = self.state / 'recoveries' / f'{recovery_id}.json'
        report = {'recovery_id': recovery_id, 'created_at': time.time(), 'status': 'checking',
                  'quarantine_cleared': False, 'record_path': str(record_path)}
        try:
            write_json(record_path, report)
            with self.locked(recovery=True):
                inflight = CONTROL_ROOT / 'inflight.json'
                pending = _recovery_bytes(inflight, optional=True)
                if pending is None:
                    report.update(status='not_needed', quarantined=False, completed_at=time.time())
                    write_json(record_path, report)
                    return {'ok': True, **report}
                checkpoint_data, checkpoint_evidence = pending
                checkpoint = _recovery_object(checkpoint_data, inflight)
                run_id = checkpoint.get('run_id')
                if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
                    raise PrismError('RECOVERY_REQUIRED', 'Recovery checkpoint has an invalid run ID.')
                report['run_id'] = run_id

                def checkpoint_path(key):
                    value = checkpoint.get(key)
                    if not isinstance(value, str) or not Path(value).is_absolute():
                        raise PrismError('RECOVERY_REQUIRED', 'Recovery checkpoint has an invalid evidence path.', field=key)
                    path = Path(value)
                    if '..' in path.parts or path.is_symlink() or path.resolve() != path:
                        raise PrismError('RECOVERY_REQUIRED', 'Recovery evidence path is not canonical.', field=key)
                    return path

                operation_path = checkpoint_path('operation')
                operation_dir = operation_path.parent
                if operation_path.name != 'operation.json' or operation_dir.name != run_id or operation_dir.parent.name != 'runs':
                    raise PrismError('RECOVERY_REQUIRED', 'Recovery checkpoint does not identify an owned operation record.')
                operation_data, operation_evidence = _recovery_bytes(operation_path)
                operation = _recovery_object(operation_data, operation_path)
                steps = operation.get('steps')
                if (operation.get('run_id') != run_id or operation.get('directory') != str(operation_dir)
                        or not isinstance(steps, list) or not steps or not isinstance(steps[-1], dict)):
                    raise PrismError('RECOVERY_REQUIRED', 'Recovery checkpoint and operation identity disagree.')
                step = steps[-1]
                number = step.get('number')
                if type(number) is not int or number < 1 or number != len(steps):
                    raise PrismError('RECOVERY_REQUIRED', 'Recovery checkpoint does not identify the latest native step.')
                step_dir = operation_dir / f'step-{number:02d}'
                script, log, done = (checkpoint_path(key) for key in ('script', 'log', 'done'))
                if (script != step_dir / 'run.pzc' or log != script.with_suffix('.log')
                        or done != step_dir / 'completed.txt' or step.get('script') != str(script)
                        or step.get('log') != str(log)):
                    raise PrismError('RECOVERY_REQUIRED', 'Recovery evidence paths do not match the owned native step.')
                token = checkpoint.get('token')
                prefix = f'{run_id}:{number}:'
                if (not isinstance(token, str) or not token.startswith(prefix)
                        or len(token[len(prefix):]) != 32
                        or any(c not in '0123456789abcdef' for c in token[len(prefix):])
                        or step.get('completion_token') != token):
                    raise PrismError('RECOVERY_REQUIRED', 'Recovery completion token does not match the native step.')
                script_data, script_evidence = _recovery_bytes(script)
                if script_evidence['sha256'] != step.get('script_sha256'):
                    raise PrismError('RECOVERY_REQUIRED', 'The native script fingerprint changed; quarantine remains.')
                try:
                    script_lines = script_data.decode('utf-8').splitlines()
                except UnicodeDecodeError as exc:
                    raise PrismError('RECOVERY_REQUIRED', 'The native script is no longer valid UTF-8.') from exc
                if f'WText {quote(token)}' not in script_lines or f'OpenOutput {quote(done)}, clear' not in script_lines:
                    raise PrismError('RECOVERY_REQUIRED', 'The native script does not contain its recorded completion output.')
                started_at_ns = step.get('started_at_ns')
                if type(started_at_ns) is not int or started_at_ns <= 0:
                    raise PrismError('RECOVERY_REQUIRED', 'The native step has no reliable dispatch timestamp.')
                log_data, log_evidence = _recovery_bytes(log)
                if log_evidence['mtime_ns'] < started_at_ns:
                    raise PrismError('RECOVERY_REQUIRED', 'The native log predates this dispatch; quarantine remains.')
                logtext = decode_log(log_data).rstrip()
                terminal_error = logtext.endswith('PROBLEM! Not completed because of error.')
                completion_data = _recovery_bytes(done, optional=True)
                complete = bool(completion_data and completion_data[1]['mtime_ns'] >= started_at_ns
                                and decode_log(completion_data[0]).strip() == token
                                and logtext.endswith('COMPLETE! No Errors.'))
                running_path = verify_running_application()
                report['application_running'] = bool(running_path)
                evidence = {'checkpoint': checkpoint_evidence, 'operation': operation_evidence,
                            'script': script_evidence, 'native_log': log_evidence}
                if completion_data is not None:
                    evidence['completion_output'] = completion_data[1]
                report['evidence'] = evidence
                if not terminal_error and not complete:
                    raise PrismError('RECOVERY_REQUIRED', 'No matching terminal native evidence; quarantine remains.',
                                     run_id=run_id, application_running=bool(running_path),
                                     native_state='unknown_or_active',
                                     process_absence_is_not_terminal_evidence=True)

                outcome = 'failed' if terminal_error else 'completed'
                if terminal_error and running_path:
                    # This existing helper targets only the named script dialog
                    # containing this owned script path. It never dismisses generic alerts.
                    Operation._dismiss_error(script)
                    report['error_dialog_dismissal'] = 'attempted_exact_script_only'
                elif terminal_error:
                    report['error_dialog_dismissal'] = 'application_not_running'

                if running_path and (native_script_windows() or step.get('native_cleanup_uncertain')):
                    raise PrismError('RECOVERY_REQUIRED', 'Terminal output exists, but native controller cleanup is unverified. Preserve open documents and close Prism before recovering this run.',
                                     run_id=run_id, native_terminal_evidence_verified=True,
                                     native_controller_cleanup_verified=False)

                # Re-read all evidence before modifying any durable operation state.
                # This also guards external writers that do not honor our OS lock.
                for item in evidence.values():
                    current = _recovery_bytes(Path(item['path']))
                    if current[1]['sha256'] != item['sha256'] or current[1]['mtime_ns'] != item['mtime_ns']:
                        raise PrismError('RECOVERY_REQUIRED', 'Recovery evidence changed before quarantine could be cleared.',
                                         path=item['path'])
                recovered_at = time.time()
                native_recovery = {'recovery_id': recovery_id, 'record_path': str(record_path),
                                   'native_outcome': outcome, 'terminal_evidence_verified': True,
                                   'semantic_completion_verified': False, 'at': recovered_at}
                prior_status = operation.get('status')
                if prior_status not in ('succeeded', 'failed'):
                    operation.update(status='failed', completed_at=recovered_at,
                                     error=PrismError('RECOVERED_UNVERIFIED_OPERATION',
                                                      'Native execution ended; the interrupted semantic operation was not verified.',
                                                      native_outcome=outcome).as_dict())
                if step.get('status') not in ('succeeded', 'failed'):
                    step.update(status='failed', completed_at=recovered_at)
                step['recovery'] = native_recovery
                operation.setdefault('recoveries', []).append(native_recovery)
                report.update(status='terminal_evidence_verified', quarantined=True,
                              native_outcome=outcome, operation_status=operation['status'],
                              prior_operation_status=prior_status, semantic_completion_verified=False,
                              completed_at=recovered_at)
                # Persist evidence first. A failure here deliberately leaves the gate.
                write_json(record_path, report)
                write_json(operation_path, operation)
                current_checkpoint = _recovery_bytes(inflight)
                if current_checkpoint[1]['sha256'] != checkpoint_evidence['sha256']:
                    raise PrismError('RECOVERY_REQUIRED', 'Global quarantine changed while recovery was recorded.')
                inflight.unlink()
                report.update(status='quarantine_cleared', quarantine_cleared=True, quarantined=False)
                write_json(record_path, report)
                return {'ok': True, **report}
        except PrismError as exc:
            report.update(status='blocked', quarantined=(CONTROL_ROOT / 'inflight.json').exists(),
                          completed_at=time.time(), error=exc.as_dict())
            exc.details.setdefault('recovery_id', recovery_id)
            exc.details.setdefault('recovery_record', str(record_path))
            try:
                write_json(record_path, report)
            except (OSError, ValueError) as record_error:
                exc.details.setdefault('recovery_record_write_error', type(record_error).__name__)
            raise
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            cleared = report['quarantine_cleared']
            error = PrismError('RECOVERY_RECORD_FAILED' if cleared else 'RECOVERY_REQUIRED',
                               'Native uncertainty was cleared, but the final recovery record could not be saved.' if cleared
                               else 'Recovery could not complete; no quarantine was released.',
                               cause=type(exc).__name__, recovery_id=recovery_id, recovery_record=str(record_path),
                               quarantine_cleared=cleared, quarantined=(CONTROL_ROOT / 'inflight.json').exists())
            report.update(status='recording_failed' if cleared else 'blocked', quarantined=(CONTROL_ROOT / 'inflight.json').exists(),
                          completed_at=time.time(), error=error.as_dict())
            try:
                write_json(record_path, report)
            except (OSError, ValueError) as record_error:
                error.details['recovery_record_write_error'] = type(record_error).__name__
            raise error from exc

    def status(self, run_id: str) -> dict:
        if not run_id or any(c not in '0123456789abcdef' for c in run_id) or len(run_id) != 32:
            raise PrismError('INVALID_RUN_ID', 'Run ID must be 32 hexadecimal characters.')
        path = self.state / 'runs' / run_id / 'operation.json'
        if not path.is_file():
            raise PrismError('RUN_NOT_FOUND', 'No recorded operation has this run ID.')
        return json.loads(path.read_text())
