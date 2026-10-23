"""Shared provider quotas, charged repairs and bounded audit reservation."""
import json
import time
from harness.runtime import run_store as store

class LimitReached(RuntimeError):
    pass


class CallBudget:
    def __init__(self, out, settings):
        self.out, self.settings, self.start = out, settings, time.monotonic()
        path = out/'limits.json'
        self.previous = json.loads(path.read_text()) if path.exists() else {'calls':0,'active_seconds':0}
        self.calls = self.previous['calls']
        self.by_purpose=dict(self.previous.get('by_purpose',{}))

    def elapsed(self):
        return self.previous['active_seconds'] + time.monotonic()-self.start

    def save(self):
        store.dump(self.out/'limits.json', {'calls':self.calls,'active_seconds':self.elapsed(),'by_purpose':self.by_purpose})

    def reserve(self,purpose="other"):
        if self.calls >= self.settings['max_model_calls']:
            raise LimitReached('total model call limit reached')
        if self.elapsed() >= self.settings['total_active_seconds']-5:
            raise LimitReached('total active time limit reached')
        if purpose=='design' and self.settings['max_model_calls']-self.calls <= self.settings.get('reserved_audit_calls',0):
            raise LimitReached('design stopped to preserve reserved proposal-audit calls within total limit')
        if purpose=='design' and self.settings['total_active_seconds']-self.elapsed() <= self.settings.get('reserved_audit_seconds',0)+5:
            raise LimitReached('design stopped to preserve reserved proposal-audit time within total limit')
        self.calls += 1
        self.by_purpose[purpose]=self.by_purpose.get(purpose,0)+1
        self.save()

    def record_observed(self,count,purpose="other"):
        """Already executed provider turns must be charged even past a limit."""
        if type(count) is not int or count<0:raise ValueError('observed count must be a nonnegative integer')
        self.calls+=count;self.by_purpose[purpose]=self.by_purpose.get(purpose,0)+count
        self.save()
        if self.calls>self.settings['max_model_calls']:
            raise LimitReached('observed provider-internal turns exceeded total model-call quota; all actual turns recorded')

    def timeout(self,purpose="other"):
        remaining = self.settings['total_active_seconds']-self.elapsed()-(self.settings.get('reserved_audit_seconds',0) if purpose=='design' else 0)
        if remaining <= 5:
            raise LimitReached('total active time limit reached')
        return min(self.settings['request_timeout_seconds'], int(remaining))

