"""来源适配层。当前只有 B 站；知乎在下一个里程碑加入。"""

from .bilibili import BilibiliSource, TextResult, is_bilibili, parse_url

__all__ = ["BilibiliSource", "TextResult", "is_bilibili", "parse_url"]
