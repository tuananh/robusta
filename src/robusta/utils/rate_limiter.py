import threading
from collections import defaultdict
from datetime import datetime


class RateLimiter:

    limiter_lock = threading.Lock()
    limiter_map = defaultdict(None)

    @staticmethod
    def mark_and_test(operation: str, id: str, period_seconds: int) -> bool:
        with RateLimiter.limiter_lock:
            limiter_key = operation + id
            last_run = RateLimiter.limiter_map.get(limiter_key)
            curr_seconds = datetime.utcnow().timestamp()
            if last_run:
                if curr_seconds - last_run > period_seconds:
                    RateLimiter.limiter_map[limiter_key] = curr_seconds
                    return True
                else:
                    return False
            else:
                RateLimiter.limiter_map[limiter_key] = curr_seconds
                return True