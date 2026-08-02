import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from webapp.services import dicts

OALD_PATH = os.path.join(dicts.DICT_DIR, dicts.DICTS["oald"])


@pytest.mark.skipif(not os.path.exists(OALD_PATH), reason="OALD MDX not installed; see docs/DICTIONARIES.md")
def test_oald_mixed_link_records_keep_real_entry():
    """OALD9 OL 版部分词条首条是 @@@LINK 跳转记录，应丢弃跳转保留真实词条。"""
    result = dicts.lookup_dict("oald", "study")
    assert result
    assert "étude" not in result
    assert "noun" in result
