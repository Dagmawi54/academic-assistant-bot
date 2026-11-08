"""Load the real worker.py with Worker-only dependencies stubbed (fastapi, pydantic, workers, sibling services)."""
import importlib.util
import os
import sys
import types
from unittest import mock


def load_worker(src_dir: str):
    src_dir = os.path.abspath(src_dir)
    for name in list(sys.modules):
        if name == "worker":
            del sys.modules[name]
    added_path = src_dir not in sys.path
    if added_path:
        sys.path.insert(0, src_dir)

    class _App:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.Header = lambda *a, **k: None
    fastapi.HTTPException = Exception
    fastapi.Request = object
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = responses.JSONResponse = responses.Response = object
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = type("BaseModel", (), {})
    pydantic.Field = lambda *a, **k: None
    workers = types.ModuleType("workers")
    workers.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
    workers.fetch = mock.AsyncMock()
    stubs = {
        "fastapi": fastapi, "fastapi.responses": responses, "pydantic": pydantic, "workers": workers,
    }
    for sibling in (
        "course_quiz_service", "course_tutor_ai", "course_tutor_service",
        "personal_task_service", "group_setup_service", "lesson_page_service",
        "poll_service", "quiz_service",
    ):
        stubs[sibling] = mock.MagicMock(name=sibling)
    previous_modules = {name: sys.modules.get(name) for name in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location("worker", os.path.join(src_dir, "worker.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules["worker"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        # Keep the imported Worker bound to its test doubles, but do not leak those doubles into
        # unrelated project tests which need the real Pydantic/FastAPI modules.
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        if added_path:
            try:
                sys.path.remove(src_dir)
            except ValueError:
                pass
