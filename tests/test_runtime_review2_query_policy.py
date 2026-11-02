"""Query dispatch must not bypass managed mutation publication accounting."""
import json

import pytest

from test_runtime_review import peers
