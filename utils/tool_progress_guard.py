"""Request-owned tool progress observations, without execution or retry policy."""
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass

_UNSET = object()


@dataclass(frozen=True)
class ToolProgress:
    reason: str
    attempts: int = 1
    elapsed: float = 0.0
    stalled: bool = False


class RequestToolProgress:
    def __init__(self, request_id, stale_seconds=45, max_records=64,
                 repeat_limit=2, clock=time.monotonic):
        self.request_id = request_id
        self.stale_seconds = max(1, float(stale_seconds))
        self.max_records = max(1, int(max_records))
        self.repeat_limit = max(2, int(repeat_limit))
        self.clock = clock
        self._records = OrderedDict()
        self._calls = OrderedDict()

    @staticmethod
    def _fingerprint(name, args):
        try:
            value = json.dumps([name, args], ensure_ascii=False, sort_keys=True,
                               separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError):
            return None
        return hashlib.sha256(value.encode()).hexdigest()

    def begin(self, call_id, name, args):
        fingerprint = self._fingerprint(name, args)
        if fingerprint is None or not isinstance(call_id, str) or not call_id:
            return ToolProgress('unrecognized')
        now = self.clock()
        record = self._records.get(fingerprint)
        if record and self._calls.get(call_id) == fingerprint:
            return self.check(call_id)
        previous = record['status'] if record else None
        attempts = record['attempts'] + 1 if record else 1
        elapsed = max(0.0, now - record['started']) if record else 0.0
        reasons = {'pending': 'duplicate_pending', 'success': 'duplicate_success',
                   'failed': 'retry_after_failure', 'unknown': 'uncertain_repeat'}
        reason = reasons.get(previous, 'started')
        # Identical inputs alone do not establish lack of progress (e.g. polling).
        stalled = previous == 'pending' and elapsed >= self.stale_seconds
        self._records[fingerprint] = {'status': 'pending', 'attempts': attempts,
                                      'started': now, 'call_id': call_id,
                                      'result_digest': record.get('result_digest') if record else None,
                                      'result_status': record.get('result_status') if record else None,
                                      'same_results': record.get('same_results', 0) if record else 0,
                                      'repeated_result': False}
        self._records.move_to_end(fingerprint)
        self._calls[call_id] = fingerprint
        while len(self._records) > self.max_records:
            old, _ = self._records.popitem(last=False)
            self._calls = OrderedDict((k, v) for k, v in self._calls.items() if v != old)
        while len(self._calls) > self.max_records:
            self._calls.popitem(last=False)
        return ToolProgress(reason, attempts, elapsed, stalled)

    def finish(self, call_id, status='success', *, result=_UNSET):
        fingerprint = self._calls.get(call_id)
        if fingerprint not in self._records or status not in {'success', 'failed', 'unknown'}:
            return ToolProgress('unrecognized')
        if self._records[fingerprint]['call_id'] != call_id:
            return ToolProgress('superseded_result', self._records[fingerprint]['attempts'])
        record = self._records[fingerprint]
        if record['status'] != 'pending':
            return self.check(call_id)
        digest = self._fingerprint('result', result) if result is not _UNSET else None
        repeated = (digest is not None and digest == record['result_digest']
                    and status == record['result_status'])
        record['same_results'] = record['same_results'] + 1 if repeated else (1 if digest else 0)
        record['repeated_result'] = record['same_results'] >= self.repeat_limit
        record['result_digest'] = digest
        record['result_status'] = status if digest else None
        record['status'] = status
        return self.check(call_id)

    def check(self, call_id=None):
        if call_id is None:
            return [self._progress(record) for record in self._records.values()]
        record = self._records.get(self._calls.get(call_id))
        if record is None:
            return ToolProgress('unrecognized')
        return self._progress(record)

    def _progress(self, record):
        elapsed = max(0.0, self.clock() - record['started'])
        stale = record['status'] == 'pending' and elapsed >= self.stale_seconds
        reason = 'stale_pending' if stale else ('repeated_result' if record['repeated_result'] else record['status'])
        return ToolProgress(reason, record['attempts'], elapsed,
                            stale or record['repeated_result'])

    def clear(self):
        self._records.clear()
        self._calls.clear()
