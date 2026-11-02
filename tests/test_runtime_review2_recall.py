"""Idle peer recall checks compatibility without inline native validation."""
import json
import threading

import pytest

from test_provider import ZvecMemoryProvider, make_provider, _mod
from test_runtime_review import ModelEngine, peers
