# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Lifecycle callback and execution telemetry decorators for ARTEMIS.

Provides function interception hooks for pre-execution logging, success verification,
and error capture across synchronous and asynchronous agent invocations.
"""

from __future__ import annotations

from functools import wraps
import inspect
from typing import Any, Callable


def wrap_with_callbacks(
    fn: Callable[..., Any] | None = None,
    *,
    before: Callable[..., None] | None = None,
    on_success: Callable[[Any], None] | None = None,
    on_failure: Callable[[Exception], None] | None = None,
    suppress_exceptions: bool = False,
) -> Any:
    """Wrap a callable with pre-invocation, success, and error callback handlers.

    Supports decorating both coroutines and regular synchronous functions.
    """

    def _decorator(target_fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(target_fn):

            @wraps(target_fn)
            async def _async_wrapped(*args: Any, **kwargs: Any) -> Any:
                if before is not None:
                    try:
                        before()
                    except Exception:
                        pass
                try:
                    result = await target_fn(*args, **kwargs)
                    if on_success is not None:
                        try:
                            on_success(result)
                        except Exception:
                            pass
                    return result
                except Exception as exc:
                    if on_failure is not None:
                        try:
                            on_failure(exc)
                        except Exception:
                            pass
                    if suppress_exceptions:
                        return None
                    raise

            return _async_wrapped

        @wraps(target_fn)
        def _sync_wrapped(*args: Any, **kwargs: Any) -> Any:
            if before is not None:
                try:
                    before()
                except Exception:
                    pass
            try:
                result = target_fn(*args, **kwargs)
                if on_success is not None:
                    try:
                        on_success(result)
                    except Exception:
                        pass
                return result
            except Exception as exc:
                if on_failure is not None:
                    try:
                        on_failure(exc)
                    except Exception:
                        pass
                if suppress_exceptions:
                    return None
                raise

        return _sync_wrapped

    if fn is not None:
        return _decorator(fn)
    return _decorator
