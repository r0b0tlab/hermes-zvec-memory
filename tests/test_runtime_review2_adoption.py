"""Legacy adoption is an identity transaction, never a stale absent snapshot."""
import threading

from test_provider import ZvecMemoryProvider, make_provider
from test_runtime_review import ModelEngine
